"""Configure only this installation's hooks through Codex's native settings API."""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from store import QueueError

PLUGIN_ID = 'codex-device-queue@codex-device-queue'
EVENTS = {'sessionStart': 'SessionStart', 'subagentStart': 'SubagentStart'}


class NativeClient:
    def __init__(self, binary, cwd):
        self.process = subprocess.Popen([binary, 'app-server', '--stdio'], cwd=str(cwd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8')
        self.lines = queue.Queue()
        self.sequence = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.process.stdin.write(json.dumps({'id': request_id, 'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QueueError('initialization_unavailable', 'Codex settings request timed out; use manual initialization')
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise QueueError('initialization_unavailable', 'Codex settings request timed out; use manual initialization')
            if line is None:
                raise QueueError('initialization_unavailable', 'Codex app-server stopped; use a Codex executable with hook settings support')
            response = json.loads(line)
            if response.get('id') != request_id:
                continue
            if 'error' in response:
                raise QueueError('initialization_unavailable', 'Codex rejected {}. Use manual initialization or retry with a supported Codex executable'.format(method))
            return response['result']

    def start(self):
        self.call('initialize', {'clientInfo': {'name': 'codex-device-queue-init', 'version': '1.0.0'},
                                 'capabilities': {'experimentalApi': True}})
        self.process.stdin.write('{"method":"initialized"}\n')
        self.process.stdin.flush()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()


def own_hooks(response, root, cwd):
    entries = [entry for entry in response.get('data', []) if Path(entry['cwd']).resolve() == cwd]
    if len(entries) != 1 or entries[0].get('errors'):
        raise QueueError('initialization_unavailable', 'Native hook discovery reported errors; use manual initialization')
    hooks = [hook for hook in entries[0].get('hooks', []) if hook.get('pluginId') == PLUGIN_ID]
    source = (root / 'hooks/hooks.json').resolve()
    if len(hooks) != 2 or {h.get('eventName') for h in hooks} != set(EVENTS):
        raise QueueError('plugin_hooks_missing', 'The two installed device-queue hooks were not discovered. Check that the plugin is installed and enabled, then retry initialization')
    for hook in hooks:
        if (hook.get('source') != 'plugin' or Path(hook['sourcePath']).resolve() != source
                or hook.get('handlerType') != 'command' or not hook.get('key') or not hook.get('currentHash')):
            raise QueueError('plugin_hooks_mismatch', 'Hook discovery does not match this plugin installation; use manual initialization')
    if len({h['key'] for h in hooks}) != 2:
        raise QueueError('plugin_hooks_mismatch', 'Duplicate hook keys; use manual initialization')
    return sorted(hooks, key=lambda h: h['key'])


def snapshot(hooks):
    return [(h['key'], h['currentHash'], h['enabled'], h['trustStatus'], h.get('isManaged')) for h in hooks]


def hook_sources(root):
    # Older app-servers cache discovery results within the process.
    return tuple((root / 'hooks' / name).read_bytes()
                 for name in ('hooks.json', 'session_start.py', 'session_start.cmd'))


def probe_hooks(root, hooks):
    """Exercise the actual OS-specific hook commands with synthetic event input."""
    definition = json.loads((root / 'hooks/hooks.json').read_text(encoding='utf-8'))['hooks']
    env = dict(os.environ, PLUGIN_ROOT=str(root), PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    for hook in hooks:
        event = EVENTS[hook['eventName']]
        handlers = definition[event]
        if len(handlers) != 1 or len(handlers[0]['hooks']) != 1:
            raise QueueError('plugin_hooks_mismatch', 'Unexpected hook definitions; use manual initialization')
        handler = handlers[0]['hooks'][0]
        command = handler['commandWindows' if os.name == 'nt' else 'command'].replace('${PLUGIN_ROOT}', str(root))
        if hook['command'] != command:
            raise QueueError('plugin_hooks_mismatch', 'Native hook command differs from the installed definition')
        result = subprocess.run(command, shell=True, cwd=str(root), env=env,
            input=json.dumps({'hook_event_name': event, 'session_id': 'device-queue-initialization-check',
                              'agent_id': 'child-check'}),
            capture_output=True, text=True, encoding='utf-8', timeout=8)
        try:
            output = json.loads(result.stdout)['hookSpecificOutput']
            owner = 'device-queue-initialization-check' + (':child-check' if event == 'SubagentStart' else '')
            valid = (output['hookEventName'] == event and owner in output['additionalContext']
                     and str(root / 'skills/device-queue/SKILL.md') in output['additionalContext'])
        except (KeyError, ValueError, TypeError):
            valid = False
        if result.returncode or not valid:
            raise QueueError('hook_probe_failed', 'The {} hook did not produce queue instructions. Check Python and the installed hook files; no hook settings were changed'.format(event))


def initialize(root, binary=None, check=False):
    root, cwd = Path(root).resolve(), Path.cwd().resolve()
    binary = binary or shutil.which('codex')
    if not binary:
        raise QueueError('initialization_unavailable', 'No Codex executable found; locate the local Codex executable or use manual initialization')
    client = None
    try:
        client = NativeClient(binary, cwd)
        client.start()
        def discover():
            return own_hooks(client.call('hooks/list', {'cwds': [str(cwd)]}), root, cwd)
        hooks = discover()
        sources = hook_sources(root)
        probe_hooks(root, hooks)
        if hook_sources(root) != sources or snapshot(discover()) != snapshot(hooks):
            raise QueueError('hooks_changed', 'Hooks changed during the execution check; review the current files and retry')
        updates = [h for h in hooks if not h['enabled'] or h['trustStatus'] not in ('trusted', 'managed')]
        changed = []
        if updates and not check:
            if any(h.get('isManaged') or h['trustStatus'] == 'managed' for h in updates):
                raise QueueError('hooks_managed', 'An administrator controls these hooks; use manual initialization with the administrator')
            config = client.call('config/read', {'includeLayers': True})
            layers = [layer for layer in config.get('layers', [])
                      if layer['name'].get('type') == 'user' and not layer['name'].get('profile')]
            if len(layers) != 1 or not layers[0].get('version'):
                raise QueueError('initialization_unavailable', 'Cannot identify the user settings version; use manual initialization')
            if hook_sources(root) != sources or snapshot(discover()) != snapshot(hooks):
                raise QueueError('hooks_changed', 'Hooks changed during initialization; review the current files and retry')
            # Use the same scoped upsert as Codex's /hooks UI, with an optimistic config version.
            client.call('config/batchWrite', {
                'edits': [{'keyPath': 'hooks.state', 'mergeStrategy': 'upsert', 'value': {
                    h['key']: {'enabled': True, 'trusted_hash': h['currentHash']} for h in updates}}],
                'filePath': layers[0]['name']['file'], 'expectedVersion': layers[0]['version'],
                'reloadUserConfig': True})
            changed = [h['eventName'] for h in updates]
            after = discover()
            if (hook_sources(root) != sources
                    or [(h['key'], h['currentHash']) for h in after] != [(h['key'], h['currentHash']) for h in hooks]):
                raise QueueError('hooks_changed', 'Hooks changed while settings were being saved; initialization is not complete')
            hooks = after
        ready = all(h['enabled'] and h['trustStatus'] in ('trusted', 'managed') for h in hooks)
        if not ready and not check:
            raise QueueError('initialization_incomplete', 'Hook settings did not become effective; use manual initialization')
        return {'state': 'ready' if ready else 'needs_initialization', 'plugin': PLUGIN_ID,
                'hooks': [{'event': h['eventName'], 'enabled': h['enabled'], 'trust': h['trustStatus']} for h in hooks],
                'updated': changed, 'hook_probe': 'passed', 'restart_required': ready}
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        raise QueueError('initialization_unavailable', 'Initialization could not verify the local Codex/hook interface ({}); use manual initialization'.format(type(error).__name__))
    finally:
        if client is not None:
            client.close()
