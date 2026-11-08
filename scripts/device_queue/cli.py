#!/usr/bin/env python3
"""Computer-wide per-device queue. Every control response is JSON."""

import argparse
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time
import uuid

from store import QueueError, Store
from runner import Interrupted, execute, group_alive, handle_signals, recover
from management import handle as manage
from storage import default_state, installed_root, migrate
from initialization import initialize

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = default_state(PLUGIN_ROOT)


def emit(value, stream=None):
    print(json.dumps(value, ensure_ascii=False), file=stream or sys.stdout, flush=True)


def wait_for(store, ticket, token, timeout):
    if timeout is not None and (timeout < 0 or not math.isfinite(timeout)):
        raise QueueError('invalid_input', 'Timeout must be a finite nonnegative number')
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            result = store.acquire(ticket, token)
        except QueueError as e:
            if e.error != 'device_busy':
                raise
            row = store.get(ticket, token)
            if row['state'] == 'granted':
                raise
            result = {'ticket': ticket, 'device': row['device'], 'state': row['state'], 'blocked_by_process': True}
        if result['state'] == 'granted':
            return result
        if deadline is not None and time.monotonic() >= deadline:
            return result
        time.sleep(0.15 if deadline is None else min(0.15, max(0, deadline - time.monotonic())))


def parser():
    p = argparse.ArgumentParser(description='Shared per-device queue (FIFO by default). Use a stable hardware ID; no device OS is assumed.')
    p.add_argument('--state-dir', type=Path, default=DEFAULT_STATE, help='Isolated tests only; real sessions must share the default')
    sub = p.add_subparsers(dest='action', required=True)
    setup = sub.add_parser('init', help="Initialize only this plugin's installed hooks; does not create queue state")
    setup.add_argument('--codex', help='Local Codex executable; defaults to codex on PATH')
    setup.add_argument('--check', action='store_true', help='Verify without changing hook settings')
    req = sub.add_parser('request', help='Register or resume this session\'s device request')
    req.add_argument('--device', required=True)
    req.add_argument('--owner', default=os.environ.get('CODEX_THREAD_ID'))
    req.add_argument('--purpose', required=True)
    req.add_argument('--front', action='store_true', help='Insert a new request at the front of this device waiting queue; never preempt its holder')
    for action in ['wait', 'release', 'cancel']:
        s = sub.add_parser(action)
        s.add_argument('--ticket', required=True)
        s.add_argument('--token', required=True)
        if action == 'wait':
            s.add_argument('--timeout', type=float, default=30, help='Seconds; a timeout leaves the ticket queued (exit 3)')
    status = sub.add_parser('status', help='List pending/granted tickets without ownership tokens')
    status.add_argument('--device')
    status.add_argument('--history', action='store_true')
    ex = sub.add_parser('exec', help='Run one command under an existing grant; stdout is command output')
    ex.add_argument('--ticket', required=True)
    ex.add_argument('--token', required=True)
    ex.add_argument('command', nargs=argparse.REMAINDER)
    run = sub.add_parser('run', help='Enqueue, wait, execute a whole script, and return the device automatically')
    run.add_argument('--device', required=True)
    run.add_argument('--owner', default=os.environ.get('CODEX_THREAD_ID'))
    run.add_argument('--purpose', required=True)
    run.add_argument('--front', action='store_true', help='Insert a new request at the front of this device waiting queue; never preempt its holder')
    run.add_argument('--wait-timeout', type=float, default=None, help='Default: wait until granted; timeout cancels this run')
    run.add_argument('command', nargs=argparse.REMAINDER)
    rec = sub.add_parser('recover', help='Recover abandoned work only after device-side idle verification')
    rec.add_argument('--ticket', required=True)
    rec.add_argument('--confirm-idle', action='store_true')
    rec.add_argument('--reason', required=True)
    management = sub.add_parser('manage', help='Internal skill adapter: resolve existing session requests without tokens')
    management.add_argument('operation', choices=['inspect', 'cancel', 'release'])
    management.add_argument('--owner', required=True)
    management.add_argument('--device')
    management.add_argument('--ticket')
    management.add_argument('--confirm-idle', action='store_true')
    management.add_argument('--reason')
    return p


def command_args(args):
    command = args.command[1:] if args.command and args.command[0] == '--' else args.command
    if not command:
        raise QueueError('invalid_input', 'Provide a command after --')
    return command


def run_command(store, args):
    command = command_args(args)
    if not args.owner:
        raise QueueError('invalid_input', 'Pass --owner outside Codex (CODEX_THREAD_ID is unset)')
    if args.wait_timeout is not None and (args.wait_timeout < 0 or not math.isfinite(args.wait_timeout)):
        raise QueueError('invalid_input', 'Timeout must be a finite nonnegative number')
    request_id = uuid.uuid4().hex
    retain_grant = False
    with handle_signals():
        try:
            ticket = store.request(args.device, args.owner, args.purpose, managed=True, request_id=request_id, front=args.front)
            if not ticket['created']:
                raise QueueError('existing_request', 'This session already has a request; resume it with wait/exec instead of starting another run')
            emit({'event': 'queued', **{k: v for k, v in ticket.items() if k != 'token'}}, sys.stderr)
            grant = wait_for(store, ticket['ticket'], ticket['token'], args.wait_timeout)
            if grant['state'] != 'granted':
                return 3
            emit({'event': 'granted', **grant}, sys.stderr)
            return execute(store, ticket['ticket'], ticket['token'], command)
        except QueueError as e:
            # A failed operation's descendant check must not become an automatic return.
            retain_grant = e.error == 'device_busy'
            raise
        except Interrupted as e:
            return 128 + e.signum
        finally:
            previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                # The ID is known before registration, including interruption after commit but before return.
                with store.transaction() as db:
                    found = db.execute('SELECT * FROM tickets WHERE ticket=?', (request_id,)).fetchone()
                    row = dict(found) if found else None
                if row and row['state'] in ('queued', 'granted') and not retain_grant:
                    result = store.close(request_id, row['token'], 'cancelled' if row['state'] == 'queued' else 'released', group_alive)
                    emit({'event': result['state'], **result}, sys.stderr)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def main():
    if os.name == 'nt':
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8')
    os.umask(0o077)
    args = parser().parse_args()
    try:
        if args.action == 'init':
            emit(initialize(PLUGIN_ROOT, args.codex, args.check))
            return 0
        installation = None
        if args.state_dir == DEFAULT_STATE:
            version_root = installed_root(PLUGIN_ROOT)
            installation = version_root.parent if version_root is not None else None
            if installation is not None and not installation.is_dir():
                raise QueueError('plugin_removed', 'The plugin installation was removed')
            migrate(Path.home() / '.codex/device-queue-state', DEFAULT_STATE, installation=installation)
        store = Store(args.state_dir, installation=installation)
        if args.action == 'run':
            return run_command(store, args)
        if args.action == 'exec':
            with handle_signals():
                try:
                    return execute(store, args.ticket, args.token, command_args(args))
                except Interrupted as e:
                    return 128 + e.signum
        if args.action == 'manage':
            result = manage(store, args.operation, args.owner, args.device, args.ticket, args.confirm_idle, args.reason)
        elif args.action == 'recover':
            result = recover(store, args.ticket, args.confirm_idle, args.reason)
        elif args.action == 'request':
            if not args.owner:
                raise QueueError('invalid_input', 'Pass --owner outside Codex (CODEX_THREAD_ID is unset)')
            result = store.request(args.device, args.owner, args.purpose, front=args.front)
        elif args.action == 'status':
            result = store.status(args.device, args.history)
        elif args.action == 'wait':
            result = wait_for(store, args.ticket, args.token, args.timeout)
            emit(result)
            return 0 if result['state'] == 'granted' else 3
        else:
            result = store.close(args.ticket, args.token, 'released' if args.action == 'release' else 'cancelled', group_alive)
        emit(result)
        return 0
    except QueueError as e:
        emit({'error': e.error, 'message': str(e)}, sys.stderr if args.action in ('run', 'exec') else sys.stdout)
        return e.code
    except (OSError, sqlite3.Error) as e:
        emit({'error': 'unavailable', 'message': str(e)})
        return 2
    except KeyboardInterrupt:
        emit({'error': 'interrupted', 'message': 'Wait interrupted; a pending request remains registered'})
        return 130


if __name__ == '__main__':
    sys.exit(main())
