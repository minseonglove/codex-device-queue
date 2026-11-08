"""Initialization uses real native hook discovery in a disposable Codex home."""
import json
import os
import queue
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_plugin import ROOT
from test_native_lifecycle import CODEX


def native_call(binary, env, cwd, method, params):
    process = subprocess.Popen([binary, 'app-server', '--stdio'], cwd=str(cwd), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8')
    lines = queue.Queue()
    def reader():
        for line in process.stdout:
            lines.put(line)
        lines.put(None)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    def call(request_id, name, arguments):
        process.stdin.write(json.dumps({'id': request_id, 'method': name, 'params': arguments}) + '\n')
        process.stdin.flush()
        while True:
            line = lines.get(timeout=20)
            if line is None:
                raise AssertionError('Native app-server exited before replying')
            response = json.loads(line)
            if response.get('id') == request_id:
                if 'error' in response:
                    raise AssertionError(response['error'])
                return response['result']
    try:
        call(1, 'initialize', {'clientInfo': {'name': 'queue-init-test', 'version': '1.0'},
                              'capabilities': {'experimentalApi': True}})
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        return call(2, method, params)
    finally:
        process.terminate()
        process.wait(timeout=5)
        thread.join(timeout=5)
        process.stdin.close()
        process.stdout.close()


@unittest.skipUnless(CODEX, 'Native Codex CLI is not installed')
class InitializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([CODEX, 'plugin', '--help'], capture_output=True,
                                text=True, encoding='utf-8', timeout=10)
        if result.returncode or 'marketplace' not in result.stdout:
            raise unittest.SkipTest('The selected Codex CLI does not support plugins')

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='queue init 기기 ')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.codex_home = self.home / '.codex'
        self.codex_home.mkdir()
        source = self.root / 'repository'
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        CODEX_HOME=str(self.codex_home), PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        for args in [('plugin', 'marketplace', 'add', str(source)),
                     ('plugin', 'add', 'codex-device-queue@codex-device-queue')]:
            result = subprocess.run([CODEX, *args], cwd=str(self.root), env=self.env,
                capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        version = json.loads((source / '.codex-plugin/plugin.json').read_text())['version']
        self.base = self.codex_home / 'plugins/cache/codex-device-queue/codex-device-queue'
        self.plugin = self.base / version
        self.cli = self.plugin / 'scripts/device_queue/cli.py'
        with (self.codex_home / 'config.toml').open('a', encoding='utf-8') as config:
            config.write('\n[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ntype="command"\ncommand="echo unrelated"\n')

    def hooks(self):
        result = native_call(CODEX, self.env, self.root, 'hooks/list', {'cwds': [str(self.root)]})
        self.assertEqual(result['data'][0]['errors'], [])
        return result['data'][0]['hooks']

    def initialize(self, *args):
        result = subprocess.run([sys.executable, str(self.cli), 'init', '--codex', CODEX, *args],
            cwd=str(self.root), env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_init_trusts_only_own_hooks_is_idempotent_and_never_creates_queue(self):
        before = self.hooks()
        own = [h for h in before if h.get('pluginId') == 'codex-device-queue@codex-device-queue']
        others = [h for h in before if not h.get('pluginId')]
        self.assertEqual(len(own), 2)
        self.assertGreater(len(others), 0)
        self.assertTrue(all(h['trustStatus'] == 'untrusted' for h in own + others))
        report = self.initialize()
        self.assertEqual(report['state'], 'ready')
        self.assertTrue(report['restart_required'])
        self.assertEqual(len(report['updated']), 2)
        after = self.hooks()
        self.assertTrue(all(h['enabled'] and h['trustStatus'] == 'trusted'
                            for h in after if h.get('pluginId') == 'codex-device-queue@codex-device-queue'))
        self.assertEqual(others, [h for h in after if not h.get('pluginId')])
        config = (self.codex_home / 'config.toml').read_bytes()
        again = self.initialize()
        self.assertEqual(again['state'], 'ready')
        self.assertEqual(again['updated'], [])
        self.assertEqual(config, (self.codex_home / 'config.toml').read_bytes())
        self.assertFalse((self.base / '.device-queue-state@shared').exists())
        self.assertFalse((self.home / '.codex/device-queue-state').exists())

    def test_check_reports_untrusted_without_changing_config(self):
        before = (self.codex_home / 'config.toml').read_bytes()
        report = self.initialize('--check')
        self.assertEqual(report['state'], 'needs_initialization')
        self.assertEqual(report['updated'], [])
        self.assertEqual(before, (self.codex_home / 'config.toml').read_bytes())
        self.assertFalse((self.base / '.device-queue-state@shared').exists())

    def test_explicit_init_reenables_disabled_own_hook_without_touching_other_settings(self):
        hook = next(h for h in self.hooks() if h.get('eventName') == 'sessionStart' and h.get('pluginId'))
        native_call(CODEX, self.env, self.root, 'config/batchWrite', {
            'edits': [{'keyPath': 'hooks.state', 'mergeStrategy': 'upsert',
                       'value': {hook['key']: {'enabled': False}}}], 'reloadUserConfig': True})
        self.assertFalse(next(h for h in self.hooks() if h['key'] == hook['key'])['enabled'])
        report = self.initialize()
        self.assertEqual(report['state'], 'ready')
        self.assertTrue(next(h for h in self.hooks() if h['key'] == hook['key'])['enabled'])

    def test_explicit_init_trusts_current_modified_hash(self):
        self.initialize()
        path = self.plugin / 'hooks/hooks.json'
        definition = json.loads(path.read_text(encoding='utf-8'))
        for groups in definition['hooks'].values():
            handler = groups[0]['hooks'][0]
            handler['command'] = handler['command'].replace('python3 ', 'python3 -u ')
            handler['commandWindows'] = handler['commandWindows'].replace('call ', 'call  ')
        path.write_text(json.dumps(definition), encoding='utf-8')
        own = [h for h in self.hooks() if h.get('pluginId')]
        self.assertTrue(all(h['trustStatus'] == 'modified' for h in own))
        report = self.initialize()
        self.assertEqual(report['state'], 'ready')
        self.assertEqual(len(report['updated']), 2)
        self.assertTrue(all(h['trustStatus'] == 'trusted' for h in self.hooks() if h.get('pluginId')))

    def test_failed_hook_probe_does_not_trust_any_hook(self):
        script = self.plugin / 'hooks/session_start.py'
        script.write_text('raise RuntimeError("broken fixture")\n', encoding='utf-8')
        before = (self.codex_home / 'config.toml').read_bytes()
        result = subprocess.run([sys.executable, str(self.cli), 'init', '--codex', CODEX],
            cwd=str(self.root), env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error'], 'hook_probe_failed')
        self.assertEqual(before, (self.codex_home / 'config.toml').read_bytes())
        self.assertTrue(all(h['trustStatus'] == 'untrusted' for h in self.hooks()))
        self.assertFalse((self.base / '.device-queue-state@shared').exists())

    def test_helper_from_other_checkout_cannot_trust_installed_hooks(self):
        before = (self.codex_home / 'config.toml').read_bytes()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/device_queue/cli.py'),
                                'init', '--codex', CODEX], cwd=str(self.root), env=self.env,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error'], 'plugin_hooks_mismatch')
        self.assertEqual(before, (self.codex_home / 'config.toml').read_bytes())

    def test_unsupported_codex_reports_error_without_writing_config_or_queue(self):
        before = (self.codex_home / 'config.toml').read_bytes()
        result = subprocess.run([sys.executable, str(self.cli), 'init', '--codex', sys.executable],
            cwd=str(self.root), env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error'], 'initialization_unavailable')
        self.assertEqual(before, (self.codex_home / 'config.toml').read_bytes())
        self.assertFalse((self.base / '.device-queue-state@shared').exists())

    def test_ready_check_rejects_hook_changes_during_probe(self):
        self.initialize()
        before = (self.codex_home / 'config.toml').read_bytes()
        import initialization
        from store import QueueError
        real_probe = initialization.probe_hooks
        def concurrent_change(root, hooks):
            real_probe(root, hooks)
            path = root / 'hooks/hooks.json'
            definition = json.loads(path.read_text(encoding='utf-8'))
            for groups in definition['hooks'].values():
                handler = groups[0]['hooks'][0]
                handler['command'] += ' '
                handler['commandWindows'] += ' '
            path.write_text(json.dumps(definition), encoding='utf-8')
        previous = Path.cwd()
        try:
            os.chdir(str(self.root))
            with patch.dict(os.environ, self.env), patch.object(initialization, 'probe_hooks', concurrent_change):
                with self.assertRaises(QueueError) as caught:
                    initialization.initialize(self.plugin, CODEX, check=True)
            self.assertEqual(caught.exception.error, 'hooks_changed')
        finally:
            os.chdir(str(previous))
        self.assertEqual(before, (self.codex_home / 'config.toml').read_bytes())
