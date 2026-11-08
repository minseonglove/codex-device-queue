"""Locks must exclude another process, including with Unicode cache/state paths."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/device_queue'))


class PortabilityTests(unittest.TestCase):
    def test_exclusive_lock_blocks_another_process_and_releases_on_close(self):
        import file_lock
        with tempfile.TemporaryDirectory(prefix='device queue ') as folder:
            path = Path(folder) / '기기.lock'
            probe = ('import os,sys; import file_lock; '
                     'f=os.open(sys.argv[1],os.O_RDWR); '
                     '\ntry: file_lock.exclusive(f, blocking=False)'
                     '\nexcept BlockingIOError: sys.exit(4)')
            env = dict(os.environ, PYTHONPATH=str(ROOT / 'scripts/device_queue'))
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                file_lock.exclusive(fd)
                p = subprocess.run([sys.executable, '-c', probe, str(path)], env=env, capture_output=True, text=True)
                self.assertEqual(p.returncode, 4, p.stderr)
            finally:
                os.close(fd)
            p = subprocess.run([sys.executable, '-c', probe, str(path)], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_hook_without_shlex_join_still_emits_usable_posix_command(self):
        import importlib.util
        import io
        import shlex
        spec = importlib.util.spec_from_file_location('queue_hook', ROOT / 'hooks/session_start.py')
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        output = io.StringIO()
        original = getattr(shlex, 'join', None)
        if original is not None:
            del shlex.join
        try:
            with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({'hook_event_name':'SessionStart','session_id':'owner spaces'}))), mock.patch.object(sys, 'stdout', output):
                hook.main()
        finally:
            if original is not None:
                shlex.join = original
        context = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
        self.assertIn('owner spaces', context)
        self.assertIn(str(ROOT / 'scripts/device_queue/cli.py'), context)

    @unittest.skipIf(os.name == 'nt', 'POSIX harness injects the Windows pre-assignment interrupt')
    def test_windows_interrupt_before_job_assignment_closes_gate_without_posix_cleanup(self):
        import runner
        from store import Store
        from types import SimpleNamespace
        import signal
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            ticket = store.request('synthetic-phone', 'A', 'gate interrupt')
            store.acquire(ticket['ticket'], ticket['token'])
            marker = Path(folder) / 'must-not-start'
            processes = []
            def spawn(path, fd, command, env):
                process = subprocess.Popen([sys.executable, path, '--worker', str(fd), str(fd), '--'] + command,
                                           pass_fds=(fd,), env=env)
                processes.append(process)
                return process
            def interrupted_job(pid):
                raise runner.Interrupted(signal.SIGINT)
            windows_os = SimpleNamespace(name='nt', pipe=os.pipe, close=os.close,
                                         write=os.write, environ=os.environ)
            try:
                with mock.patch.object(runner, 'os', windows_os), mock.patch.dict(sys.modules, {'windows_backend': SimpleNamespace(spawn_worker=spawn, Job=interrupted_job)}):
                    with self.assertRaises(runner.Interrupted):
                        runner.execute(store, ticket['ticket'], ticket['token'],
                            [sys.executable, '-c', 'from pathlib import Path; Path(%r).touch()' % str(marker)])
                self.assertFalse(marker.exists())
                self.assertEqual(processes[0].returncode, 125)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
