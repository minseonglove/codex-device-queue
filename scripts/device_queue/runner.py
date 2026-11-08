"""Execute commands only under a grant; retain ownership for surviving work."""

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from store import QueueError


class Interrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum


@contextmanager
def handle_signals():
    def interrupt(signum, frame):
        raise Interrupted(signum)
    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def process_alive(pid):
    if os.name == 'nt':
        from windows_backend import process_alive as windows_process_alive
        return windows_process_alive(pid)
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def group_alive(pgid):
    if os.name == 'nt':
        from windows_backend import group_alive as windows_group_alive
        return windows_group_alive(pgid)
    result = subprocess.run(['/bin/ps', '-axo', 'pgid=,stat='], capture_output=True, text=True, timeout=5)
    if result.returncode or not result.stdout.strip():
        raise QueueError('unavailable', 'Cannot establish process-group liveness')
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            raise QueueError('unavailable', 'Unexpected process table; refusing to assume the device is idle')
        if int(fields[0]) == pgid and not fields[1].startswith('Z'):
            return True
    return False


def stop_group(p):
    for sig, duration in [(signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)]:
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            p.poll()
            if not group_alive(p.pid):
                p.wait(timeout=1)
                return
            time.sleep(0.05)
    raise QueueError('device_busy', 'Unable to stop the registered process group; grant remains held', 4)


def worker(lock_fd, gate_fd, command):
    # Work cannot start in the crash window between spawn and durable registration.
    with os.fdopen(gate_fd, 'rb', buffering=0) as gate:
        if gate.read(1) != b'G':
            return 125
    try:
        p = subprocess.Popen(command, **({} if lock_fd is None else {'pass_fds': (lock_fd,)}))
    except FileNotFoundError:
        print('device-queue: command not found', file=sys.stderr, flush=True)
        return 127
    except OSError as e:
        print(f'device-queue: {e}', file=sys.stderr, flush=True)
        return 126
    code = p.wait()
    return code if code >= 0 else 128 - code


def execute(store, ticket, token, command):
    if not command:
        raise QueueError('invalid_input', 'Provide a command after --')
    row = store.get(ticket, token)
    with store.device_lock(row['device']) as lock_fd:
        with store.transaction() as db:
            current = store.lookup(db, ticket, token)
            if current['state'] != 'granted':
                raise QueueError('not_granted', 'An exclusive grant is required before command execution')
            if any(group_alive(g) for g in store.active_groups(db, ticket)):
                raise QueueError('device_busy', 'An earlier operation still has active descendants', 4)
        read_fd, write_fd = os.pipe()
        p = None
        operation = None
        job = None
        started = False
        try:
            env = os.environ.copy()
            env['DEVICE_QUEUE_DEVICE'] = row['device']
            env['DEVICE_QUEUE_TICKET'] = ticket
            if os.name == 'nt':
                from windows_backend import Job, spawn_worker
                p = spawn_worker(str(Path(__file__).resolve()), read_fd, command, env)
                job = Job(p.pid)
            else:
                p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', str(lock_fd), str(read_fd), '--', *command], start_new_session=True, pass_fds=(lock_fd, read_fd), env=env)
            os.close(read_fd)
            read_fd = None
            with store.transaction() as db:
                store.lookup(db, ticket, token)
                operation = db.execute("INSERT INTO operations(ticket,pgid,state,started) VALUES(?,?,'running',?)", (ticket, p.pid, time.time())).lastrowid
            os.write(write_fd, b'G')
            started = True
            os.close(write_fd)
            write_fd = None
            code = p.wait()
            if group_alive(p.pid):
                raise QueueError('device_busy', 'Command leader exited but descendants remain; grant is retained', 4)
            with store.transaction() as db:
                db.execute("UPDATE operations SET state='finished',ended=? WHERE id=?", (time.time(), operation))
            return code if code >= 0 else 128 - code
        except (Interrupted, KeyboardInterrupt):
            if p is not None:
                # Avoid further interrupts during the bounded cleanup of the owned group.
                previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
                try:
                    if job is not None:
                        job.stop()
                        p.wait(timeout=3)
                    elif os.name != 'nt':
                        stop_group(p)
                    if operation is not None:
                        with store.transaction() as db:
                            db.execute("UPDATE operations SET state='finished',ended=? WHERE id=?", (time.time(), operation))
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)
            raise
        finally:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)
            if job is not None:
                job.close()
            if p is not None:
                if not started:
                    # EOF or a closed job stops a worker that never received permission.
                    p.wait(timeout=5)
                else:
                    p.poll()


def recover(store, ticket, confirmed, reason):
    if not confirmed or not reason.strip():
        raise QueueError('confirmation_required', 'Recovery requires --confirm-idle and a reason after verifying device-side activity has stopped')
    with store.transaction() as db:
        row = dict(store.lookup(db, ticket))
    with store.device_lock(row['device']):
        with store.transaction() as db:
            row = store.lookup(db, ticket)
            if row['state'] not in ('queued', 'granted'):
                return store.view(db, row)
            if process_alive(row['controller_pid']):
                raise QueueError('device_busy', 'The managed run controller is still alive', 4)
            if any(group_alive(g) for g in store.active_groups(db, ticket)):
                raise QueueError('device_busy', 'A registered process group is still active', 4)
            db.execute("UPDATE operations SET state='finished',ended=? WHERE ticket=? AND state='running'", (time.time(), ticket))
            db.execute("UPDATE tickets SET state='recovered',updated=? WHERE ticket=?", (time.time(), ticket))
            result = store.view(db, store.lookup(db, ticket))
            result['reason'] = reason
            return result


if __name__ == '__main__':
    if len(sys.argv) >= 5 and sys.argv[1] == '--windows-worker' and sys.argv[3] == '--':
        from windows_backend import gate_descriptor
        sys.exit(worker(None, gate_descriptor(int(sys.argv[2])), sys.argv[4:]))
    if len(sys.argv) >= 6 and sys.argv[1] == '--worker' and sys.argv[4] == '--':
        sys.exit(worker(int(sys.argv[2]), int(sys.argv[3]), sys.argv[5:]))
    sys.exit(2)
