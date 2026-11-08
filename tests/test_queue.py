import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / 'scripts' / 'device_queue' / 'cli.py'


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.addCleanup(self.tmp.cleanup)

    def command(self, *args):
        return [sys.executable, str(CLI), '--state-dir', str(self.state), *args]

    def call(self, *args, code=0):
        p = subprocess.run(self.command(*args), text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        self.assertTrue(p.stdout.strip(), 'Missing JSON control response: ' + p.stderr)
        return json.loads(p.stdout)

    def request(self, owner, device='phone-1'):
        return self.call('request', '--device', device, '--owner', owner, '--purpose', 'test')

    def auth(self, ticket):
        return ['--ticket', ticket['ticket'], '--token', ticket['token']]

    def acquire(self, ticket, code=0):
        return self.call('wait', *self.auth(ticket), '--timeout', '0', code=code)

    def release(self, ticket):
        return self.call('release', *self.auth(ticket))

class QueueTests(QueueCase):
    def test_fifo_requires_explicit_grant_and_never_skips_head(self):
        a, b, c = [self.request(x) for x in ['A', 'B', 'C']]
        self.assertEqual(a['state'], 'queued')
        self.assertEqual(self.acquire(c, code=3)['state'], 'queued')
        self.assertEqual(self.acquire(a)['state'], 'granted')
        self.assertEqual(self.acquire(b, code=3)['position'], 2)
        self.release(a)
        self.assertEqual(self.acquire(b)['state'], 'granted')
        self.assertEqual(self.acquire(c, code=3)['state'], 'queued')
        self.release(b)
        self.assertEqual(self.acquire(c)['state'], 'granted')
        self.release(c)

    def test_distinct_devices_have_independent_owners(self):
        a = self.request('A', 'phone-1')
        b = self.request('B', 'phone-2')
        self.acquire(a)
        self.acquire(b)
        active = self.call('status')['tickets']
        self.assertEqual([r['state'] for r in active], ['granted', 'granted'])
        self.release(a)
        self.release(b)

    def test_retry_reuses_request_and_token_without_changing_fifo(self):
        a = self.request('A')
        self.request('B')
        retry = self.request('A')
        self.assertEqual(retry['ticket'], a['ticket'])
        self.assertEqual(retry['token'], a['token'])
        self.assertFalse(retry['created'])
        self.assertEqual(len(self.call('status')['tickets']), 2)
        self.assertEqual(retry['position'], 1)

    def test_wrong_token_cannot_grant_cancel_execute_or_release(self):
        a = self.request('A')
        wrong = ['--ticket', a['ticket'], '--token', 'wrong']
        for verb in ['wait', 'cancel', 'release']:
            self.assertEqual(self.call(verb, *wrong, code=2)['error'], 'unauthorized')
        self.assertEqual(self.call('status')['tickets'][0]['state'], 'queued')

    def test_status_does_not_disclose_capability(self):
        a = self.request('A')
        view = self.call('status', '--device', 'phone-1')
        self.assertNotIn(a['token'], json.dumps(view))
        self.assertEqual(view['tickets'][0]['owner'], 'A')

    def test_cancelled_head_does_not_block_next_waiter(self):
        a, b = self.request('A'), self.request('B')
        self.assertEqual(self.call('cancel', *self.auth(a))['state'], 'cancelled')
        self.acquire(b)
        self.assertEqual(self.call('cancel', *self.auth(b), code=2)['error'], 'already_granted')
        self.release(b)

    def test_invalid_identifiers_cannot_create_an_unaddressable_queue(self):
        for device in ['', ' ', 'phone\nother', ' phone']:
            self.assertEqual(self.call('request', '--device', device, '--owner', 'A',
                                       '--purpose', 'test', code=2)['error'], 'invalid_input')
        self.assertEqual(self.call('status')['tickets'], [])

    def test_concurrent_registration_is_unique_and_complete(self):
        processes = [subprocess.Popen(self.command('request', '--device', 'same',
                      '--owner', str(i), '--purpose', 'race'), text=True, encoding='utf-8',
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(12)]
        for p in processes:
            out, err = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0, out + err)
        rows = self.call('status', '--device', 'same')['tickets']
        self.assertEqual(len(rows), 12)
        self.assertEqual(len({r['ticket'] for r in rows}), 12)
        self.assertEqual([r['position'] for r in rows], list(range(1, 13)))

    def test_state_is_shared_from_different_working_directories(self):
        a = self.request('A')
        p = subprocess.run(self.command('status'), cwd='/', capture_output=True,
                           text=True, encoding='utf-8', timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['tickets'][0]['ticket'], a['ticket'])


if __name__ == '__main__':
    unittest.main()

class EdgeTests(QueueCase):
    def test_non_ascii_wrong_token_is_a_controlled_refusal(self):
        a = self.request('A')
        self.assertEqual(self.call('wait', '--ticket', a['ticket'], '--token', '\ud2c0\ub9bc', '--timeout', '0', code=2)['error'], 'unauthorized')

    def test_duplicate_concurrent_retries_create_one_ticket(self):
        processes = [subprocess.Popen(self.command('request', '--device', 'same', '--owner', 'A', '--purpose', 'retry'), text=True, encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
        tickets = []
        for p in processes:
            out, err = p.communicate(timeout=10)
            self.assertEqual(p.returncode, 0, out + err)
            tickets.append(json.loads(out)['ticket'])
        self.assertEqual(len(set(tickets)), 1)
        self.assertEqual(len(self.call('status')['tickets']), 1)

    def test_simultaneous_acquisitions_grant_only_fifo_head(self):
        a, b = self.request('A'), self.request('B')
        processes = [subprocess.Popen(self.command('wait', *self.auth(t), '--timeout', '0'), text=True, encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE) for t in [b, a]]
        results = []
        for p in processes:
            out, err = p.communicate(timeout=10)
            self.assertIn(p.returncode, [0, 3], out + err)
            results.append(json.loads(out))
        self.assertEqual([r['state'] for r in results], ['queued', 'granted'])
        self.release(a)
        self.acquire(b)
        self.release(b)

    def test_generated_token_can_be_passed_back_as_a_normal_cli_argument(self):
        # URL-safe randomness can begin with '-', which argparse otherwise treats as an option.
        script = f"""
import sys
sys.path.insert(0, {str(CLI.parent)!r})
import cli, store
store.secrets.token_urlsafe = lambda n: '-leading-hyphen-fixture'
sys.argv = ['device-queue', '--state-dir', {str(self.state)!r}, 'request', '--device', 'phone-1', '--owner', 'A', '--purpose', 'token boundary']
sys.exit(cli.main())
"""
        p = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        a = json.loads(p.stdout)
        self.acquire(a)
        self.release(a)
