import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

from test_queue import QueueCase, CLI


class RunnerTests(QueueCase):
    # Replacing FIFO gating, signal cleanup, or process tracking must break these integration tests.
    def until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail('Timed out waiting for subprocess evidence')

    def spawn(self, *args):
        p = subprocess.Popen(self.command(*args), text=True, encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if p.poll() is None:
                p.terminate()
            try:
                p.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate(timeout=8)
        self.addCleanup(cleanup)
        return p

    def run_args(self, device, owner, script):
        return ['run', '--device', device, '--owner', owner, '--purpose', 'integration', '--', sys.executable, '-c', script]

    def test_run_waits_automatically_and_other_device_runs_in_parallel(self):
        folder = Path(self.tmp.name)
        events, gate = folder / 'events', folder / 'gate'
        script_a = f"from pathlib import Path; import time; p=Path({str(events)!r}); p.write_text('A-start\\n'); gate=Path({str(gate)!r});\nwhile not gate.exists(): time.sleep(.03)\nwith p.open('a') as f: f.write('A-end\\n')"
        a = self.spawn(*self.run_args('phone-1', 'A', script_a))
        self.until(events.exists)
        b = self.spawn(*self.run_args('phone-1', 'B', f"from pathlib import Path; p=Path({str(events)!r});\nwith p.open('a') as f: f.write('B\\n')"))
        self.until(lambda: len(self.call('status', '--device', 'phone-1')['tickets']) == 2)
        c = subprocess.run(self.command(*self.run_args('phone-2', 'C', "print('other-device')")), capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(c.returncode, 0, c.stderr)
        self.assertEqual(c.stdout.strip(), 'other-device')
        self.assertEqual(events.read_text(), 'A-start\n')
        gate.touch()
        for p in [a, b]:
            out, err = p.communicate(timeout=8)
            self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(events.read_text(), 'A-start\nA-end\nB\n')
        self.assertEqual(self.call('status')['tickets'], [])

    def test_failed_command_returns_its_exit_code_and_releases(self):
        p = subprocess.run(self.command(*self.run_args('phone-1', 'A', 'raise SystemExit(7)')), capture_output=True, text=True, encoding='utf-8', timeout=8)
        self.assertEqual(p.returncode, 7, p.stderr)
        self.assertEqual(self.call('status')['tickets'], [])

    def test_missing_command_does_not_leak_grant(self):
        p = subprocess.run(self.command('run', '--device', 'phone-1', '--owner', 'A', '--purpose', 'missing', '--', '/no/such/device-command'), capture_output=True, text=True, encoding='utf-8', timeout=8)
        self.assertEqual(p.returncode, 127, p.stderr)
        self.assertEqual(self.call('status')['tickets'], [])

    @unittest.skipIf(os.name == 'nt', 'POSIX signal/group contract; Windows crash/job behavior has separate tests')
    def test_sigterm_stops_registered_processes_before_releasing(self):
        marker = Path(self.tmp.name) / 'started'
        p = self.spawn(*self.run_args('phone-1', 'A', f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(60)"))
        self.until(marker.exists)
        p.terminate()
        out, err = p.communicate(timeout=8)
        self.assertEqual(p.returncode, 143, out + err)
        self.assertEqual(self.call('status')['tickets'], [])
        b = self.request('B')
        self.acquire(b)
        self.release(b)

    @unittest.skipIf(os.name == 'nt', 'POSIX signal/group contract; Windows crash/job behavior has separate tests')
    def test_sigkill_does_not_handoff_while_orphan_command_runs(self):
        folder = Path(self.tmp.name)
        marker, done = folder / 'started', folder / 'done'
        p = self.spawn(*self.run_args('phone-1', 'A', f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(1.5); Path({str(done)!r}).touch()"))
        self.until(marker.exists)
        a = self.call('status')['tickets'][0]
        p.kill()
        p.wait(timeout=3)
        b = self.request('B')
        self.acquire(b, code=3)
        self.assertEqual(self.call('recover', '--ticket', a['ticket'], '--confirm-idle', '--reason', 'test recovery', code=4)['error'], 'device_busy')
        self.until(done.exists)
        p.communicate(timeout=5)
        self.call('recover', '--ticket', a['ticket'], '--confirm-idle', '--reason', 'local command finished')
        self.acquire(b)
        self.release(b)

    def test_manual_exec_refuses_release_during_work_and_checks_tokens(self):
        marker = Path(self.tmp.name) / 'started'
        a = self.request('A')
        self.acquire(a)
        wrong = subprocess.run(self.command('exec', '--ticket', a['ticket'], '--token', 'wrong', '--', sys.executable, '-c', f"from pathlib import Path; Path({str(marker)!r}).touch()"), capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(wrong.returncode, 2, wrong.stderr)
        self.assertFalse(marker.exists())
        p = self.spawn('exec', *self.auth(a), '--', sys.executable, '-c', f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(.8)")
        self.until(marker.exists)
        self.assertEqual(self.call('release', *self.auth(a), code=4)['error'], 'device_busy')
        out, err = p.communicate(timeout=5)
        self.assertEqual(p.returncode, 0, out + err)
        self.release(a)

    @unittest.skipIf(os.name == 'nt', 'POSIX signal/group contract; Windows crash/job behavior has separate tests')
    def test_descendants_still_block_release_when_the_leader_exits(self):
        marker = Path(self.tmp.name) / 'started'
        done = Path(self.tmp.name) / 'done'
        child = f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(1.3); Path({str(done)!r}).touch()"
        script = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}],close_fds=True)"
        a = self.request('A')
        self.acquire(a)
        p = self.spawn('exec', *self.auth(a), '--', sys.executable, '-c', script)
        self.until(marker.exists)
        p.wait(timeout=5)
        self.assertEqual(p.returncode, 4)
        self.assertEqual(self.call('release', *self.auth(a), code=4)['error'], 'device_busy')
        self.until(done.exists)
        p.communicate(timeout=5)
        self.release(a)

    def test_recovery_requires_explicit_confirmation_even_without_processes(self):
        a = self.request('A')
        self.acquire(a)
        self.assertEqual(self.call('recover', '--ticket', a['ticket'], '--reason', 'unknown state', code=2)['error'], 'confirmation_required')
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'granted')
        self.release(a)

    def test_run_never_takes_over_existing_session_ticket(self):
        a = self.request('A')
        p = subprocess.run(self.command(*self.run_args('phone-1', 'A', "print('must not run')")), capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertNotIn('must not run', p.stdout)
        self.assertEqual(self.call('status')['tickets'][0]['ticket'], a['ticket'])
        self.call('cancel', *self.auth(a))

    @unittest.skipIf(os.name == 'nt', 'POSIX signal/group contract; Windows crash/job behavior has separate tests')
    def test_worker_cannot_start_device_work_before_registration_handshake(self):
        marker = Path(self.tmp.name) / 'must-not-start'
        lock = os.open(Path(self.tmp.name) / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
        read_fd, write_fd = os.pipe()
        try:
            p = subprocess.Popen([sys.executable, str(CLI.parent / 'runner.py'), '--worker', str(lock), str(read_fd), '--', sys.executable, '-c', f"from pathlib import Path; Path({str(marker)!r}).touch()"], pass_fds=(lock, read_fd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            os.close(write_fd)
            write_fd = None
            out, err = p.communicate(timeout=5)
            self.assertEqual(p.returncode, 125, out + err)
            self.assertFalse(marker.exists())
        finally:
            os.close(lock)
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def test_wait_timeout_of_run_cancels_only_its_own_ticket(self):
        a = self.request('A')
        self.acquire(a)
        p = subprocess.run(self.command('run', '--device', 'phone-1', '--owner', 'B', '--purpose', 'timeout', '--wait-timeout', '0.05', '--', sys.executable, '-c', "print('must-not-run')"), capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(p.stdout, '')
        active = self.call('status')['tickets']
        self.assertEqual([r['owner'] for r in active], ['A'])
        self.release(a)

    @unittest.skipIf(os.name == 'nt', 'POSIX signal/group contract; Windows crash/job behavior has separate tests')
    def test_sigterm_while_queued_removes_waiter(self):
        a = self.request('A')
        self.acquire(a)
        p = self.spawn(*self.run_args('phone-1', 'B', "print('must-not-run')"))
        self.until(lambda: len(self.call('status')['tickets']) == 2)
        p.terminate()
        out, err = p.communicate(timeout=5)
        self.assertEqual(p.returncode, 143, out + err)
        self.assertEqual([r['owner'] for r in self.call('status')['tickets']], ['A'])
        self.release(a)

    def test_exec_cannot_run_under_a_queued_ticket(self):
        marker = Path(self.tmp.name) / 'must-not-run'
        a = self.request('A')
        p = subprocess.run(self.command('exec', *self.auth(a), '--', sys.executable, '-c', f"from pathlib import Path; Path({str(marker)!r}).touch()"), capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse(marker.exists())

    def managed_holder(self):
        marker = Path(self.tmp.name) / 'holder-started'
        p = self.spawn(*self.run_args('phone-1', 'holder', f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(60)"))
        self.until(marker.exists)
        return p

    def test_cancel_pending_ticket_while_another_managed_command_holds_device(self):
        self.managed_holder()
        b = self.request('B')
        self.assertEqual(self.call('cancel', *self.auth(b))['state'], 'cancelled')
        self.assertEqual([r['owner'] for r in self.call('status')['tickets']], ['holder'])

    def test_run_timeout_does_not_leak_pending_ticket_behind_managed_command(self):
        self.managed_holder()
        p = subprocess.run(self.command('run', '--device', 'phone-1', '--owner', 'B', '--purpose', 'timeout behind active work', '--wait-timeout', '.05', '--', sys.executable, '-c', "print('must-not-run')"), capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual([r['owner'] for r in self.call('status')['tickets']], ['holder'])

    @unittest.skipIf(os.name == 'nt', 'POSIX signal/group contract; Windows crash/job behavior has separate tests')
    def test_sigterm_does_not_leak_pending_ticket_behind_managed_command(self):
        self.managed_holder()
        p = self.spawn(*self.run_args('phone-1', 'B', "print('must-not-run')"))
        self.until(lambda: len(self.call('status')['tickets']) == 2)
        p.terminate()
        out, err = p.communicate(timeout=5)
        self.assertEqual(p.returncode, 143, out + err)
        self.assertEqual([r['owner'] for r in self.call('status')['tickets']], ['holder'])

    def interrupted_registration(self, point):
        argv = ['device-queue', '--state-dir', str(self.state), *self.run_args('phone-1', 'A', "print('must-not-run')")]
        script = f"""
import os, signal, sys
sys.path.insert(0, {str(CLI.parent)!r})
import cli
sys.argv = {argv!r}
if {point!r} == 'emit':
    original = cli.emit
    def emit(value, stream=None):
        original(value, stream)
        if value.get('event') == 'queued':
            os.kill(os.getpid(), signal.SIGTERM)
    cli.emit = emit
else:
    original = cli.Store.request
    def request(*args, **kwargs):
        value = original(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGTERM)
        return value
    cli.Store.request = request
sys.exit(cli.main())
"""
        p = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(p.returncode, 143, p.stdout + p.stderr)
        self.assertEqual(self.call('status')['tickets'], [])

    @unittest.skipIf(os.name == 'nt', 'POSIX signal/group contract; Windows crash/job behavior has separate tests')
    def test_sigterm_at_queued_event_cleans_up_registered_request(self):
        self.interrupted_registration('emit')

    @unittest.skipIf(os.name == 'nt', 'POSIX signal/group contract; Windows crash/job behavior has separate tests')
    def test_sigterm_after_commit_before_request_returns_still_cleans_up(self):
        self.interrupted_registration('request-return')

    def test_urgent_run_waits_for_current_work_then_precedes_ordinary_waiter(self):
        folder = Path(self.tmp.name)
        events, gate = folder / 'front-events', folder / 'front-gate'
        script = f"from pathlib import Path; import time; p=Path({str(events)!r}); p.write_text('A-start\\n'); gate=Path({str(gate)!r});\nwhile not gate.exists(): time.sleep(.02)\nwith p.open('a') as f: f.write('A-end\\n')"
        a = self.spawn(*self.run_args('phone-1', 'A', script))
        self.until(events.exists)
        normal = self.spawn(*self.run_args('phone-1', 'normal', f"from pathlib import Path;\nwith Path({str(events)!r}).open('a') as f: f.write('normal\\n')"))
        self.until(lambda: len(self.call('status')['tickets']) == 2)
        urgent_args = self.run_args('phone-1', 'urgent', f"from pathlib import Path;\nwith Path({str(events)!r}).open('a') as f: f.write('urgent\\n')")
        urgent_args.insert(1, '--front')
        urgent = self.spawn(*urgent_args)
        self.until(lambda: len(self.call('status')['tickets']) == 3)
        rows = self.call('status')['tickets']
        self.assertEqual([(r['owner'], r['state'], r['position']) for r in rows], [('A', 'granted', 1), ('urgent', 'queued', 2), ('normal', 'queued', 3)])
        self.assertIsNone(a.poll())
        self.assertEqual(events.read_text(), 'A-start\n')
        gate.touch()
        for p in [a, urgent, normal]:
            out, err = p.communicate(timeout=8)
            self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(events.read_text(), 'A-start\nA-end\nurgent\nnormal\n')
        self.assertEqual(self.call('status')['tickets'], [])
