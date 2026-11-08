"""Resolve existing session requests without exposing capabilities or registering work."""

from runner import group_alive
from store import QueueError, identifier


def handle(store, action, owner, device=None, ticket=None, confirm_idle=False, reason=None):
    identifier(owner, 'Session owner')
    if action == 'inspect':
        grouped = {}
        rows = store.status(device)['tickets']
        held_devices = {row['device'] for row in rows if row['state'] == 'granted'}
        for row in rows:
            if ticket is not None and row['ticket'] != ticket:
                continue
            row['current_session'] = row['owner'] == owner
            group = grouped.setdefault(row['device'], {'device': row['device'], 'holder': None, 'waiting': []})
            if row['state'] == 'granted':
                group['holder'] = row
            else:
                row['waiting_position'] = row['position'] - int(row['device'] in held_devices)
                group['waiting'].append(row)
        return {'outcome': 'status', 'devices': list(grouped.values())}
    if action not in ('cancel', 'release'):
        raise QueueError('invalid_input', 'Unknown management action')
    candidates = store.owned_requests(owner, device, ticket)
    if ticket is None:
        matching = [row for row in candidates if row['state'] == ('queued' if action == 'cancel' else 'granted')]
        if matching:
            candidates = matching
    if not candidates:
        return {'outcome': 'no_request'}
    if len(candidates) > 1:
        return {'outcome': 'selection_required', 'candidates': candidates}
    selected = candidates[0]
    if selected['state'] not in ('queued', 'granted'):
        return {'outcome': 'already_closed', 'request': selected}
    if action == 'release':
        if not confirm_idle or not reason or not reason.strip():
            raise QueueError('confirmation_required', 'Verify this session\'s local, UI/MCP and device-side activity ended; supply --confirm-idle and --reason with actual evidence')
    result = store.close_owned(selected['ticket'], owner, 'cancelled' if action == 'cancel' else 'released', group_alive)
    outcome = result['state'] if result['state'] in ('cancelled', 'released') else 'already_closed'
    return {'outcome': outcome, 'request': result}
