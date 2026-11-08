"""Native Codex cache lifecycle, isolated from the user's installation and queue."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from test_plugin import ROOT
sys.path.insert(0, str(ROOT / 'scripts/device_queue'))
from store import Store

CODEX = os.environ.get('DEVICE_QUEUE_TEST_CODEX') or shutil.which('codex')


@unittest.skipUnless(CODEX, 'Native Codex CLI is not installed')
class NativeLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([CODEX, 'plugin', '--help'], capture_output=True,
                                text=True, encoding='utf-8', timeout=10)
        if result.returncode or 'marketplace' not in result.stdout:
            raise unittest.SkipTest('The selected Codex CLI does not support plugins')

    def test_update_preserves_active_run_and_standard_removal_deletes_all_state(self):
        with tempfile.TemporaryDirectory(prefix='queue native lifecycle ') as folder:
            root = Path(folder).resolve()
            home, source = root / 'home', root / 'repository'
            home.mkdir()
            codex_home = home / '.codex'
            codex_home.mkdir()
            shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('.git', '__pycache__'))
            env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex_home),
                       PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
            def native(*args):
                result = subprocess.run([CODEX, *args], cwd=str(root), env=env,
                    capture_output=True, text=True, encoding='utf-8', timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result
            catalog = json.loads((source / '.agents/plugins/marketplace.json').read_text(encoding='utf-8'))
            plugin_id = 'codex-device-queue@' + catalog['name']
            native('plugin', 'marketplace', 'add', str(source))
            native('plugin', 'add', plugin_id)
            manifest = source / '.codex-plugin/plugin.json'
            version = json.loads(manifest.read_text(encoding='utf-8'))['version']
            base = codex_home / 'plugins/cache' / catalog['name'] / 'codex-device-queue'
            def cli(*args):
                result = subprocess.run([sys.executable, str(base / version / 'scripts/device_queue/cli.py'), *args],
                    cwd=str(root), env=env, capture_output=True, text=True, encoding='utf-8', timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)
            legacy_path = home / '.codex/device-queue-state'
            legacy = Store(legacy_path)
            closed = legacy.request('SYNTHETIC-OLD', 'old-fixture', 'closed legacy history')
            legacy.close(closed['ticket'], closed['token'], 'cancelled', lambda _: False)
            a = cli('request', '--device', 'SYNTHETIC-TABLET', '--owner', 'A', '--purpose', 'native lifecycle fixture')
            cli('cancel', '--ticket', a['ticket'], '--token', a['token'])
            self.assertFalse(legacy_path.exists())
            marker, gate = root / 'command-started', root / 'finish-command'
            script = ('from pathlib import Path; import time; Path(%r).touch(); gate=Path(%r); '
                      'deadline=time.monotonic()+20;\nwhile not gate.exists() and time.monotonic()<deadline: time.sleep(.02)') % (str(marker), str(gate))
            process = subprocess.Popen([sys.executable, str(base / version / 'scripts/device_queue/cli.py'),
                'run', '--device', 'SYNTHETIC-RUN', '--owner', 'during-update', '--purpose', 'update fixture',
                '--', sys.executable, '-c', script], cwd=str(root), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), 'The synthetic command did not start')
                info = json.loads(manifest.read_text(encoding='utf-8'))
                major, minor, patch = info['version'].split('.')
                next_version = '{}.{}.{}'.format(major, minor, int(patch) + 1)
                info['version'] = next_version
                manifest.write_text(json.dumps(info), encoding='utf-8')
                native('plugin', 'add', plugin_id)
                version = next_version
                gate.touch()
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, output + error)
            finally:
                gate.touch()
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)
            self.assertEqual(cli('status')['tickets'], [])
            history = cli('status', '--history')['tickets']
            self.assertTrue({a['ticket'], closed['ticket']} <= {row['ticket'] for row in history})
            self.assertTrue(all(row['state'] in ('cancelled', 'released') for row in history))
            self.assertEqual(len(list(base.glob('.legacy-device-queue-state@retired-*'))), 1)
            listed = json.loads(native('plugin', 'list', '--json').stdout)
            self.assertEqual(listed['installed'][0]['version'], next_version)
            state = base / '.device-queue-state@shared'
            self.assertTrue((state / 'queue.sqlite3').is_file())
            native('plugin', 'remove', plugin_id, '--json')
            self.assertFalse(base.exists())
            self.assertFalse(state.exists())
            self.assertFalse(legacy_path.exists())
