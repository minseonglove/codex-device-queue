"""Inject queue instructions without reserving hardware or changing user files."""

import json
import os
from pathlib import Path
import shlex
import sys


def shell_command(args):
    if os.name == 'nt':
        # Codex's interactive Windows shell is commonly PowerShell; label it explicitly.
        return '& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in args)
    return ' '.join(shlex.quote(arg) for arg in args)


def main():
    event = json.load(sys.stdin)
    name = event.get('hook_event_name')
    if name not in ('SessionStart', 'SubagentStart'):
        return
    root = Path(__file__).resolve().parents[1]
    skill = root / 'skills/device-queue/SKILL.md'
    cli = root / 'scripts/device_queue/cli.py'
    command = shell_command([sys.executable, '-X', 'utf8', str(cli)])
    owner = event.get('session_id', '')
    if name == 'SubagentStart' and event.get('agent_id'):
        owner += ':' + event['agent_id']
    owner_hint = ('Session owner (JSON string): ' + json.dumps(owner) + '; pass it unchanged as --owner on request/run and management operations. ') if owner else ''
    context = (
        'MANDATORY PHYSICAL-DEVICE COORDINATION: Before installation, execution, testing, '
        'debugging, UI/MCP interaction or test-data capture on any connected physical device, '
        f'read the complete skill at {skill} and its linked rules. '
        f'Queue CLI ({"PowerShell" if os.name == "nt" else "POSIX shell"}): {command}. {owner_hint}'
        'Automatically enqueue, wait for an explicit exclusive grant, use only the selected '
        'stable hardware ID, and release after device activity stops. One shared queue per '
        'physical device, across projects, worktrees and sessions sharing this plugin installation. FIFO by default; add '
        '--front to a new request/run when the user asks for priority in any language or '
        'concrete evidence establishes urgency, explaining the reason in --purpose. '
        'Never interrupt an existing grant. Preserve ticket/token on retries. '
        'Do not bypass the queue because a device looks idle, notify peers instead of '
        'acquiring a grant, or use --state-dir for real work. If exclusive ownership cannot '
        'be established, continue independent work without accessing the device. '
        'Side-effect-free device discovery, local builds and simulator-only work are exempt. '
        'For queue status, cancellation, return or recovery, use the focused device-queue-status, '
        'device-queue-cancel, device-queue-release or device-queue-recover skill. Execute the '
        'internal helper and report its result; never ask the user to run Python or paste tokens. '
        'Always display the full stable device ID; add names only from actual discovery. '
        'Startup does not reserve a device. This plugin does not wake inactive chats.'
    )
    print(json.dumps({'hookSpecificOutput': {'hookEventName': name, 'additionalContext': context}}))


if __name__ == '__main__':
    main()
