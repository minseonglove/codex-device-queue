import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts/device_queue/cli.py'


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='device queue 기기 ')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), PYTHONIOENCODING='utf-8', CODEX_HOME=str(self.home / 'alternate-codex'))

    def test_source_default_uses_configured_codex_cache(self):
        # A source checkout uses the same removable cache boundary as its installation.
        probe = 'import sys; sys.path.insert(0, sys.argv[1]); import cli; print(cli.DEFAULT_STATE)'
        p = subprocess.run([sys.executable, '-c', probe, str(CLI.parent)], env=self.env,
                           capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertEqual(p.stdout.strip(), str(self.home / 'alternate-codex/plugins/cache/codex-device-queue/codex-device-queue/.device-queue-state@shared'))

    def test_relocated_copies_share_the_default_queue(self):
        # Plugin versions must contend even when loaded from different cache paths.
        other = Path(self.temp.name) / 'another plugin cache'
        shutil.copytree(ROOT, other, ignore=shutil.ignore_patterns('__pycache__', '.git'))
        def call(path, *args):
            p = subprocess.run([sys.executable, str(path), *args], env=self.env,
                               capture_output=True, text=True, encoding='utf-8', timeout=8)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return json.loads(p.stdout)
        a = call(CLI, 'request', '--device', 'TEST-USB-42', '--owner', 'A', '--purpose', 'synthetic')
        grant = call(CLI, 'wait', '--ticket', a['ticket'], '--token', a['token'], '--timeout', '0')
        self.assertEqual(grant['state'], 'granted')
        b = call(other / 'scripts/device_queue/cli.py', 'request', '--device', 'TEST-USB-42',
                 '--owner', 'B', '--purpose', 'synthetic')
        self.assertEqual(b['position'], 2)
        self.assertTrue((self.home / 'alternate-codex/plugins/cache/codex-device-queue/codex-device-queue/.device-queue-state@shared/queue.sqlite3').is_file())
        self.assertTrue((self.home / 'alternate-codex').exists())
        call(CLI, 'release', '--ticket', a['ticket'], '--token', a['token'])
        call(CLI, 'cancel', '--ticket', b['ticket'], '--token', b['token'])

    def test_hook_context_survives_spaces_and_does_not_touch_queue_state(self):
        # Broken shell quoting fails startup in a cache path with spaces.
        relocated = Path(self.temp.name) / 'plugin with spaces'
        shutil.copytree(ROOT, relocated, ignore=shutil.ignore_patterns('__pycache__', '.git'))
        hooks = json.loads((relocated / 'hooks/hooks.json').read_text())['hooks']
        env = dict(self.env, PLUGIN_ROOT=str(relocated), PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
        for event, extra in [('SessionStart', {'source': 'compact'}), ('SubagentStart', {'agent_id': 'child-2'})]:
            handler = hooks[event][0]['hooks'][0]
            command = handler['commandWindows' if os.name == 'nt' else 'command'].replace('${PLUGIN_ROOT}', str(relocated))
            p = subprocess.run(command, shell=True, env=env,
                               input=json.dumps({'hook_event_name': event, 'session_id': 'test-session', **extra}),
                               capture_output=True, text=True, encoding='utf-8', timeout=5)
            self.assertEqual(p.returncode, 0, p.stderr)
            output = json.loads(p.stdout)['hookSpecificOutput']
            self.assertEqual(output['hookEventName'], event)
            context = output['additionalContext']
            self.assertIn(str(relocated / 'skills/device-queue/SKILL.md'), context)
            self.assertIn(str(relocated / 'scripts/device_queue/cli.py'), context)
            self.assertIn('test-session:child-2' if event == 'SubagentStart' else 'test-session', context)
        self.assertFalse((self.home / '.codex').exists())
        self.assertFalse((self.home / 'alternate-codex').exists())
