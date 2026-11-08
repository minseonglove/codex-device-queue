"""Native Win32 tests: execute these on Windows, not through WSL."""
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from test_queue import QueueCase, CLI


@unittest.skipUnless(os.name == 'nt', 'Requires the native Windows backend')
class WindowsTests(QueueCase):
    def until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.03)
        self.fail('Timed out waiting for native Windows work')

    def test_liveness_probe_never_terminates_the_target(self):
        sys.path.insert(0, str(CLI.parent))
        import runner
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            self.assertTrue(runner.process_alive(p.pid))
            self.assertIsNone(p.poll())
        finally:
            p.kill()
            p.wait(timeout=5)
        self.assertFalse(runner.process_alive(p.pid))

    def test_worker_waits_for_gate_and_rejects_eof(self):
        sys.path.insert(0, str(CLI.parent))
        import windows_backend as win
        marker = Path(self.tmp.name) / 'must not start'
        read_fd, write_fd = os.pipe()
        try:
            p = win.spawn_worker(str(CLI.parent / 'runner.py'), read_fd,
                [sys.executable, '-c', 'from pathlib import Path; Path(%r).touch()' % str(marker)], os.environ.copy())
            os.close(write_fd)
            write_fd = None
            self.assertEqual(p.wait(timeout=8), 125)
            self.assertFalse(marker.exists())
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def test_killed_controller_stops_descendants_and_retains_grant(self):
        folder = Path(self.tmp.name)
        marker, done = folder / 'started', folder / 'escaped'
        child = 'from pathlib import Path; import time; Path(%r).touch(); time.sleep(2); Path(%r).touch()' % (str(marker), str(done))
        script = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",%r]); time.sleep(60)' % child
        p = subprocess.Popen(self.command('run', '--device', 'win-phone', '--owner', 'A', '--purpose', 'native crash test', '--', sys.executable, '-c', script), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        try:
            self.until(marker.exists)
            a = self.call('status')['tickets'][0]
            p.kill()
            p.communicate(timeout=8)
            self.assertEqual(self.call('status')['tickets'][0]['state'], 'granted')
            b = self.request('B', 'win-phone')
            self.acquire(b, code=3)
            # A surviving descendant would write this marker after two seconds.
            time.sleep(2.2)
            self.assertFalse(done.exists())
            self.call('recover', '--ticket', a['ticket'], '--confirm-idle', '--reason', 'verified synthetic processes stopped')
            self.acquire(b)
            self.release(b)
        finally:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=8)

    def test_job_assignment_failure_cannot_start_command(self):
        marker = Path(self.tmp.name) / 'must not start'
        a = self.request('A')
        self.acquire(a)
        script = '''
import sys
sys.path.insert(0, %r)
import cli, windows_backend
class RefusedJob:
    def __init__(self, pid): raise OSError('synthetic assignment refusal')
windows_backend.Job = RefusedJob
sys.argv = %r
sys.exit(cli.main())
''' % (str(CLI.parent), ['queue', '--state-dir', str(self.state), 'exec', *self.auth(a), '--', sys.executable, '-c', 'from pathlib import Path; Path(%r).touch()' % str(marker)])
        p = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, encoding='utf-8', timeout=8)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'granted')
        self.release(a)

    def test_leader_exit_stops_descendants_but_retains_managed_grant(self):
        folder = Path(self.tmp.name)
        marker, done = folder / 'child-started', folder / 'child-escaped'
        child = 'from pathlib import Path; import time; Path(%r).touch(); time.sleep(2); Path(%r).touch()' % (str(marker), str(done))
        script = ('import subprocess,sys,time; from pathlib import Path; '
                  'subprocess.Popen([sys.executable,"-c",%r]); '
                  'marker=Path(%r); '
                  '\nwhile not marker.exists(): time.sleep(.02)') % (child, str(marker))
        p = subprocess.run(self.command('run', '--device', 'win-phone', '--owner', 'A',
            '--purpose', 'leader exit test', '--', sys.executable, '-c', script),
            capture_output=True, text=True, encoding='utf-8', timeout=8)
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertTrue(marker.exists())
        a = self.call('status')['tickets'][0]
        self.assertEqual(a['state'], 'granted')
        time.sleep(2.2)
        self.assertFalse(done.exists())
        self.call('recover', '--ticket', a['ticket'], '--confirm-idle', '--reason',
                  'verified synthetic child was stopped')
        self.assertEqual(self.call('status')['tickets'], [])
