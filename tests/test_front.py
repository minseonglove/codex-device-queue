import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from test_queue import QueueCase, CLI


class DeviceIsolationTests(QueueCase):
    def test_same_session_can_hold_two_independent_device_grants(self):
        a = self.request('same-session', 'device-A')
        b = self.request('same-session', 'device-B')
        waiting_a = self.request('other-session', 'device-A')
        self.acquire(a)
        self.acquire(b)
        self.assertEqual(self.acquire(waiting_a, code=3)['position'], 2)
        self.release(a)
        self.acquire(waiting_a)
        rows = self.call('status')['tickets']
        self.assertEqual({(r['device'], r['owner'], r['state']) for r in rows},
                         {('device-A', 'other-session', 'granted'), ('device-B', 'same-session', 'granted')})
        self.release(waiting_a)
        self.release(b)

    def test_interleaved_requests_have_separate_fifo_positions_and_handoffs(self):
        a1 = self.request('A1', 'device-A')
        b1 = self.request('B1', 'device-B')
        a2 = self.request('A2', 'device-A')
        b2 = self.request('B2', 'device-B')
        self.assertEqual([a1['position'], b1['position'], a2['position'], b2['position']], [1, 1, 2, 2])
        self.acquire(a1)
        self.acquire(b1)
        self.release(b1)
        self.acquire(b2)
        self.assertEqual(self.acquire(a2, code=3)['position'], 2)
        rows_a = self.call('status', '--device', 'device-A')['tickets']
        self.assertEqual([(r['owner'], r['state']) for r in rows_a], [('A1', 'granted'), ('A2', 'queued')])
        self.release(a1)
        self.acquire(a2)
        self.release(a2)
        self.release(b2)


class FrontInsertionTests(QueueCase):
    def urgent(self, owner, device='phone-1'):
        return self.call('request', '--device', device, '--owner', owner, '--purpose', 'urgent test', '--front')

    def owners(self, device='phone-1'):
        return [r['owner'] for r in self.call('status', '--device', device)['tickets']]

    def test_front_request_precedes_pending_jobs_but_does_not_grant_itself(self):
        a, b = self.request('A'), self.request('B')
        urgent = self.urgent('urgent')
        self.assertEqual((urgent['state'], urgent['position']), ('queued', 1))
        self.assertEqual(self.owners(), ['urgent', 'A', 'B'])
        self.acquire(a, code=3)
        self.acquire(urgent)
        self.release(urgent)
        self.acquire(a)
        self.release(a)
        self.acquire(b)
        self.release(b)

    def test_granted_owner_stays_first_even_between_device_commands(self):
        a = self.request('A')
        self.acquire(a)
        b = self.request('B')
        urgent = self.urgent('urgent')
        self.assertEqual(urgent['position'], 2)
        self.assertEqual(self.owners(), ['A', 'urgent', 'B'])
        self.acquire(urgent, code=3)
        self.assertEqual(self.acquire(a)['state'], 'granted')
        self.release(a)
        self.acquire(b, code=3)
        self.acquire(urgent)
        self.release(urgent)
        self.acquire(b)
        self.release(b)

    def test_multiple_front_inserts_prepend_without_reordering_normal_jobs(self):
        self.request('normal-1')
        self.urgent('urgent-1')
        self.request('normal-2')
        latest = self.urgent('urgent-2')
        self.request('normal-3')
        self.assertEqual(self.owners(), ['urgent-2', 'urgent-1', 'normal-1', 'normal-2', 'normal-3'])
        self.acquire(latest)
        self.release(latest)
        self.assertEqual(self.owners(), ['urgent-1', 'normal-1', 'normal-2', 'normal-3'])

    def test_front_retry_reuses_ticket_and_preserves_existing_position(self):
        a, b = self.request('A'), self.request('B')
        self.urgent('urgent')
        retry = self.urgent('B')
        self.assertEqual((retry['ticket'], retry['token']), (b['ticket'], b['token']))
        self.assertFalse(retry['created'])
        self.assertEqual(self.owners(), ['urgent', 'A', 'B'])
        self.assertEqual(retry['position'], 3)

    def test_front_insert_is_scoped_to_one_device(self):
        a = self.request('A', 'device-A')
        self.request('A-next', 'device-A')
        b = self.request('B', 'device-B')
        self.acquire(a)
        urgent = self.urgent('urgent-B', 'device-B')
        self.assertEqual(urgent['position'], 1)
        self.assertEqual(self.owners('device-A'), ['A', 'A-next'])
        self.acquire(urgent)
        self.release(urgent)
        self.acquire(b)
        self.release(b)
        self.assertEqual(self.call('status', '--device', 'device-A')['tickets'][0]['state'], 'granted')
        self.release(a)

    def test_concurrent_front_inserts_have_distinct_positions_and_no_lost_requests(self):
        self.request('normal')
        processes = [subprocess.Popen(self.command('request', '--device', 'phone-1', '--owner', f'urgent-{i}', '--purpose', 'race', '--front'), text=True, encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(8)]
        for p in processes:
            out, err = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0, out + err)
        rows = self.call('status', '--device', 'phone-1')['tickets']
        self.assertEqual(len({r['ticket'] for r in rows}), 9)
        self.assertEqual(rows[-1]['owner'], 'normal')
        self.assertEqual([r['position'] for r in rows], list(range(1, 10)))

    def test_legacy_database_preserves_grant_tokens_and_existing_queue(self):
        self.state.mkdir()
        with sqlite3.connect(self.state / 'queue.sqlite3') as db:
            db.execute('''CREATE TABLE tickets (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ticket TEXT NOT NULL UNIQUE,
                token TEXT NOT NULL, device TEXT NOT NULL, owner TEXT NOT NULL,
                purpose TEXT NOT NULL, state TEXT NOT NULL,
                managed INTEGER NOT NULL DEFAULT 0, controller_pid INTEGER,
                created REAL NOT NULL, updated REAL NOT NULL
            )''')
            db.executemany('INSERT INTO tickets(ticket,token,device,owner,purpose,state,created,updated) VALUES(?,?,?,?,?,?,1,1)', [
                ('old-held', 'token-held', 'phone-1', 'held', 'legacy', 'granted'),
                ('old-pending', 'token-pending', 'phone-1', 'pending', 'legacy', 'queued'),
                ('old-closed', 'token-closed', 'phone-1', 'closed', 'legacy', 'released')])
        urgent = self.urgent('urgent')
        self.assertEqual(self.owners(), ['held', 'urgent', 'pending'])
        self.acquire(urgent, code=3)
        self.release({'ticket': 'old-held', 'token': 'token-held'})
        self.acquire(urgent)
        self.release(urgent)
        pending = {'ticket': 'old-pending', 'token': 'token-pending'}
        self.acquire(pending)
        self.release(pending)
        rows = self.call('status', '--history')['tickets']
        self.assertEqual({r['ticket'] for r in rows}, {'old-held', 'old-pending', 'old-closed', urgent['ticket']})

    def test_legacy_grant_update_cannot_skip_front_request_or_current_holder(self):
        ordinary = self.request('ordinary')
        urgent = self.urgent('urgent')
        # This is the grant UPDATE issued by the deployed FIFO-only client.
        with sqlite3.connect(self.state / 'queue.sqlite3') as db:
            db.execute("UPDATE tickets SET state='granted',updated=2 WHERE ticket=?", (ordinary['ticket'],))
        self.assertEqual([(r['owner'], r['state']) for r in self.call('status')['tickets']], [('urgent', 'queued'), ('ordinary', 'queued')])
        self.acquire(urgent)
        with sqlite3.connect(self.state / 'queue.sqlite3') as db:
            db.execute("UPDATE tickets SET state='granted',updated=3 WHERE ticket=?", (ordinary['ticket'],))
        self.assertEqual([(r['owner'], r['state']) for r in self.call('status')['tickets']], [('urgent', 'granted'), ('ordinary', 'queued')])
        self.release(urgent)
        with sqlite3.connect(self.state / 'queue.sqlite3') as db:
            db.execute("UPDATE tickets SET state='granted',updated=4 WHERE ticket=?", (ordinary['ticket'],))
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'granted')
        self.release(ordinary)

    def test_legacy_insert_without_order_appends_after_all_existing_waiters(self):
        self.request('normal-1')
        self.request('normal-2')
        self.urgent('urgent')
        with sqlite3.connect(self.state / 'queue.sqlite3') as db:
            db.execute("INSERT INTO tickets(ticket,token,device,owner,purpose,state,managed,controller_pid,created,updated) VALUES('legacy-new','legacy-token','phone-1','legacy-owner','old client','queued',0,NULL,1,1)")
        self.assertEqual(self.owners(), ['urgent', 'normal-1', 'normal-2', 'legacy-owner'])
