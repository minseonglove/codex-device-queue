import json
import subprocess
import sys
from pathlib import Path

from test_queue import QueueCase, CLI


class ManagementCase(QueueCase):
    def manage(self, action, owner='A', *extra, code=0):
        return self.call('manage', action, '--owner', owner, *extra, code=code)

    def release_owned(self, owner='A', *extra, code=0):
        return self.manage('release', owner, '--confirm-idle', '--reason', 'Synthetic work is stopped', *extra, code=code)


class ManagementTests(ManagementCase):
    def test_cancel_existing_wait_without_a_remembered_token(self):
        a = self.request('A')
        other = self.request('B')
        result = self.manage('cancel')
        self.assertEqual(result['outcome'], 'cancelled')
        self.assertEqual(result['request']['ticket'], a['ticket'])
        self.assertNotIn(a['token'], json.dumps(result))
        self.assertNotIn('token', json.dumps(result))
        self.assertEqual([r['ticket'] for r in self.call('status')['tickets']], [other['ticket']])

    def test_no_request_is_a_noop_and_never_registers_replacement_work(self):
        for action in ['cancel', 'release']:
            self.assertEqual(self.manage(action)['outcome'], 'no_request')
        self.assertEqual(self.call('status', '--history')['tickets'], [])

    def test_multiple_device_requests_require_selection_without_mutation(self):
        a = self.request('A', 'ANDROID-EXAMPLE-01')
        b = self.request('A', 'ANDROID-EXAMPLE-02')
        result = self.manage('cancel')
        self.assertEqual(result['outcome'], 'selection_required')
        self.assertEqual({r['device'] for r in result['candidates']}, {'ANDROID-EXAMPLE-01', 'ANDROID-EXAMPLE-02'})
        self.assertEqual(len(self.call('status')['tickets']), 2)
        result = self.manage('cancel', 'A', '--device', 'ANDROID-EXAMPLE-02', '--ticket', b['ticket'])
        self.assertEqual(result['request']['ticket'], b['ticket'])
        self.assertEqual([r['ticket'] for r in self.call('status')['tickets']], [a['ticket']])

    def test_exact_ticket_owned_by_another_session_is_refused(self):
        b = self.request('B')
        result = self.manage('cancel', 'A', '--ticket', b['ticket'], code=2)
        self.assertEqual(result['error'], 'unauthorized')
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'queued')

    def test_subagent_cannot_reuse_parent_owner_request(self):
        parent = self.request('session')
        self.assertEqual(self.manage('cancel', 'session:child')['outcome'], 'no_request')
        self.assertEqual(self.manage('cancel', 'session:child', '--ticket', parent['ticket'], code=2)['error'], 'unauthorized')

    def test_granted_request_cannot_be_cancelled_as_waiting_work(self):
        a = self.request('A')
        self.acquire(a)
        self.assertEqual(self.manage('cancel', code=2)['error'], 'already_granted')
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'granted')
        self.release(a)

    def test_release_requires_evidence_and_never_releases_a_queued_request(self):
        a = self.request('A')
        self.assertEqual(self.release_owned(code=2)['error'], 'not_granted')
        self.acquire(a)
        self.assertEqual(self.manage('release', code=2)['error'], 'confirmation_required')
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'granted')
        result = self.release_owned()
        self.assertEqual(result['outcome'], 'released')
        self.assertNotIn(a['token'], json.dumps(result))
        self.assertEqual(self.call('status')['tickets'], [])

    def test_closed_ticket_is_not_replaced_by_a_new_active_request(self):
        old = self.request('A')
        self.call('cancel', *self.auth(old))
        current = self.request('A')
        result = self.manage('cancel', 'A', '--ticket', old['ticket'])
        self.assertEqual(result['outcome'], 'already_closed')
        self.assertEqual(result['request']['ticket'], old['ticket'])
        self.assertEqual([r['ticket'] for r in self.call('status')['tickets']], [current['ticket']])
        self.assertEqual(len(self.call('status', '--history')['tickets']), 2)

    def test_ticket_device_mismatch_is_refused_without_mutation(self):
        a = self.request('A', 'device-1')
        self.assertEqual(self.manage('cancel', 'A', '--device', 'device-2', '--ticket', a['ticket'], code=2)['error'], 'target_mismatch')
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'queued')

    def test_inspect_keeps_same_model_device_ids_separate_and_never_invents_names(self):
        a = self.request('A', 'ANDROID-EXAMPLE-01')
        self.acquire(a)
        self.request('B', 'ANDROID-EXAMPLE-01')
        b = self.request('A', 'ANDROID-EXAMPLE-02')
        result = self.manage('inspect')
        self.assertEqual(result['outcome'], 'status')
        devices = result['devices']
        self.assertEqual([d['device'] for d in devices], ['ANDROID-EXAMPLE-01', 'ANDROID-EXAMPLE-02'])
        self.assertTrue(devices[0]['holder']['current_session'])
        self.assertFalse(devices[0]['waiting'][0]['current_session'])
        self.assertEqual(devices[0]['waiting'][0]['waiting_position'], 1)
        self.assertIsNone(devices[1]['holder'])
        self.assertEqual(devices[1]['waiting'][0]['ticket'], b['ticket'])
        serialized = json.dumps(result)
        for value in [a['token'], b['token'], 'connected', 'model', 'running', 'token']:
            self.assertNotIn(value, serialized)
        self.release(a)

    def test_empty_inspect_reports_no_known_queues_without_adding_hardware(self):
        self.assertEqual(self.manage('inspect')['devices'], [])
        self.assertEqual(self.call('status', '--history')['tickets'], [])

    def test_queued_cancel_does_not_need_the_current_holders_execution_lock(self):
        a = self.request('A')
        b = self.request('B')
        self.acquire(b, code=3)
        self.acquire(a)
        sys.path.insert(0, str(CLI.parent))
        from store import Store
        with Store(self.state).device_lock('phone-1'):
            self.assertEqual(self.manage('cancel', 'B')['outcome'], 'cancelled')
            self.assertEqual(self.release_owned('A', code=4)['error'], 'device_busy')
        self.release(a)


class ManagementRaceTests(QueueCase):
    def modules(self):
        sys.path.insert(0, str(CLI.parent))
        from management import handle
        from store import Store, QueueError
        return handle, Store, QueueError

    def test_cancel_rechecks_grant_acquired_after_target_selection(self):
        handle, Store, QueueError = self.modules()
        request = self.request('A')
        class RacingStore(Store):
            def owned_requests(inner, *args, **kwargs):
                snapshot = super().owned_requests(*args, **kwargs)
                inner.acquire(request['ticket'], request['token'])
                return snapshot
        with self.assertRaises(QueueError) as error:
            handle(RacingStore(self.state), 'cancel', 'A')
        self.assertEqual(error.exception.error, 'already_granted')
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'granted')
        self.release(request)

    def test_release_cannot_skip_idle_evidence_when_selection_was_queued(self):
        handle, Store, QueueError = self.modules()
        request = self.request('A')
        class RacingStore(Store):
            def owned_requests(inner, *args, **kwargs):
                snapshot = super().owned_requests(*args, **kwargs)
                inner.acquire(request['ticket'], request['token'])
                return snapshot
        with self.assertRaises(QueueError) as error:
            handle(RacingStore(self.state), 'release', 'A')
        self.assertEqual(error.exception.error, 'confirmation_required')
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'granted')
        self.release(request)

    def test_request_closed_after_selection_never_cancels_its_replacement(self):
        handle, Store, _ = self.modules()
        original = self.request('A')
        class RacingStore(Store):
            def owned_requests(inner, *args, **kwargs):
                snapshot = super().owned_requests(*args, **kwargs)
                inner.close(original['ticket'], original['token'], 'cancelled', lambda _: False)
                inner.request('phone-1', 'A', 'replacement')
                return snapshot
        result = handle(RacingStore(self.state), 'cancel', 'A')
        self.assertEqual(result['request']['ticket'], original['ticket'])
        rows = self.call('status')['tickets']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['purpose'], 'replacement')

    def test_owner_is_checked_again_inside_release_mutation(self):
        from contextlib import contextmanager
        handle, Store, QueueError = self.modules()
        request = self.request('A')
        self.acquire(request)
        class RacingStore(Store):
            @contextmanager
            def device_lock(inner, device):
                with super().device_lock(device) as fd:
                    with inner.transaction() as db:
                        db.execute('UPDATE tickets SET owner=? WHERE ticket=?', ('B', request['ticket']))
                    yield fd
        with self.assertRaises(QueueError) as error:
            handle(RacingStore(self.state), 'release', 'A', confirm_idle=True, reason='synthetic stopped work')
        self.assertEqual(error.exception.error, 'unauthorized')
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'granted')
        self.release(request)


class ManagementSelectionTests(ManagementCase):
    def test_cancel_selects_the_only_waiter_when_this_owner_also_holds_another_device(self):
        held = self.request('A', 'device-held')
        self.acquire(held)
        waiting = self.request('A', 'device-waiting')
        result = self.manage('cancel')
        self.assertEqual(result['outcome'], 'cancelled')
        self.assertEqual(result['request']['ticket'], waiting['ticket'])
        self.assertEqual(self.call('status')['tickets'][0]['ticket'], held['ticket'])
        self.release(held)

    def test_release_selects_the_only_holder_when_this_owner_also_waits_for_another_device(self):
        held = self.request('A', 'device-held')
        self.acquire(held)
        waiting = self.request('A', 'device-waiting')
        result = self.release_owned()
        self.assertEqual(result['outcome'], 'released')
        self.assertEqual(result['request']['ticket'], held['ticket'])
        self.assertEqual(self.call('status')['tickets'][0]['ticket'], waiting['ticket'])

    def test_inspect_ticket_filter_preserves_actual_waiting_position(self):
        a = self.request('A')
        self.acquire(a)
        self.request('B')
        c = self.request('C')
        result = self.manage('inspect', 'C', '--ticket', c['ticket'])
        self.assertEqual(result['devices'][0]['waiting'][0]['waiting_position'], 2)
        self.release(a)


class ManagedWaitCancellationTests(ManagementCase):
    def test_cancel_owner_wait_stops_managed_waiter_without_running_its_command(self):
        import time
        a = self.request('holder')
        self.acquire(a)
        marker = Path(self.tmp.name) / 'must-not-run'
        process = subprocess.Popen(self.command('run', '--device', 'phone-1', '--owner', 'waiter',
                                                '--purpose', 'synthetic waiting job', '--', sys.executable,
                                                '-c', 'from pathlib import Path; Path(%r).touch()' % str(marker)),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        try:
            deadline = time.monotonic() + 5
            while len(self.call('status')['tickets']) < 2 and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertEqual(len(self.call('status')['tickets']), 2)
            result = self.manage('cancel', 'waiter')
            self.assertEqual(result['outcome'], 'cancelled')
            _, err = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 2, err)
            self.assertFalse(marker.exists())
            self.assertEqual(self.call('status')['tickets'][0]['owner'], 'holder')
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
            self.release(a)
