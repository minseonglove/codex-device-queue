"""Queue state must follow native Codex cache lifetime, not plugin versions."""
import os
import errno
from pathlib import Path
import sqlite3
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from test_queue import CLI
sys.path.insert(0, str(CLI.parent))
from store import Store, QueueError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='queue storage ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def storage(self):
        import storage
        return storage

    def test_installed_versions_share_cache_state_despite_environment_overrides(self):
        storage = self.storage()
        base = self.root / 'codex/plugins/cache/local-market/codex-device-queue'
        a, b = base / '0.3.0', base / '0.3.1'
        with mock.patch.dict(os.environ, {'CODEX_HOME':str(self.root / 'other-home')}):
            self.assertEqual(storage.default_state(a), base / '.device-queue-state@shared')
            self.assertEqual(storage.default_state(b), base / '.device-queue-state@shared')

    def test_source_checkout_uses_configured_codex_installation(self):
        storage = self.storage()
        home = self.root / 'custom-codex'
        with mock.patch.dict(os.environ, {'CODEX_HOME':str(home)}):
            self.assertEqual(storage.default_state(self.root / 'repo'), home / 'plugins/cache/codex-device-queue/codex-device-queue/.device-queue-state@shared')

    def test_closed_legacy_history_is_migrated_and_old_records_removed(self):
        storage = self.storage()
        legacy, target = self.root / 'old-state', self.root / 'cache/.device-queue-state@shared'
        old = Store(legacy)
        ticket = old.request('synthetic-phone', 'A', 'completed fixture')
        old.close(ticket['ticket'], ticket['token'], 'cancelled', lambda _: False)
        storage.migrate(legacy, target)
        self.assertFalse(legacy.exists())
        rows = Store(target).status(history=True)['tickets']
        self.assertEqual([(r['ticket'],r['state']) for r in rows], [(ticket['ticket'],'cancelled')])
        self.assertTrue((target.parent / '.queue-migration.lock').exists())

    def test_active_legacy_request_blocks_migration_without_creating_second_queue(self):
        storage = self.storage()
        legacy, target = self.root / 'old-state', self.root / 'cache/state'
        old = Store(legacy)
        ticket = old.request('synthetic-phone', 'A', 'active fixture')
        with self.assertRaises(QueueError) as error:
            storage.migrate(legacy, target)
        self.assertEqual(error.exception.error, 'device_busy')
        self.assertFalse(target.exists())
        self.assertEqual(old.status()['tickets'][0]['ticket'], ticket['ticket'])

    def test_inherited_legacy_lock_prevents_migration(self):
        storage = self.storage()
        legacy, target = self.root / 'old-state', self.root / 'cache/state'
        old = Store(legacy)
        with old.device_lock('orphan'):
            with self.assertRaises(QueueError) as error:
                storage.migrate(legacy, target)
            self.assertEqual(error.exception.error, 'device_busy')
        self.assertFalse(target.exists())

    def test_two_existing_queue_databases_are_never_merged_or_overwritten(self):
        storage = self.storage()
        legacy, target = self.root / 'old-state', self.root / 'cache/state'
        Store(legacy)
        newer = Store(target)
        request = newer.request('synthetic-phone', 'B', 'keep newer state')
        with self.assertRaises(QueueError) as error:
            storage.migrate(legacy, target)
        self.assertEqual(error.exception.error, 'storage_conflict')
        self.assertEqual(newer.status()['tickets'][0]['ticket'], request['ticket'])

    def test_unexpected_legacy_files_are_preserved(self):
        storage = self.storage()
        legacy, target = self.root / 'old-state', self.root / 'cache/state'
        Store(legacy)
        foreign = legacy / 'other-app-data'
        foreign.write_text('keep')
        with self.assertRaises(QueueError):
            storage.migrate(legacy, target)
        self.assertEqual(foreign.read_text(), 'keep')
        self.assertFalse(target.exists())

    def test_loaded_store_does_not_recreate_removed_database(self):
        folder = self.root / 'state'
        old = Store(folder)
        (folder / 'queue.sqlite3').unlink()
        with self.assertRaises(QueueError):
            old.status()
        self.assertFalse((folder / 'queue.sqlite3').exists())

    def test_installed_store_refuses_transactions_after_version_is_removed(self):
        installation = self.root / 'cache/0.3.0'
        installation.mkdir(parents=True)
        store = Store(self.root / 'cache/state', installation=installation)
        installation.rmdir()
        with self.assertRaises(QueueError) as error:
            store.request('synthetic-phone','A','must not register')
        self.assertEqual(error.exception.error, 'plugin_removed')

    def test_retry_refuses_replaced_destination_and_preserves_legacy_history(self):
        storage = self.storage()
        legacy, target = self.root / 'old-state', self.root / 'cache/state'
        old = Store(legacy)
        ticket = old.request('synthetic-phone','A','history to preserve')
        old.close(ticket['ticket'],ticket['token'],'cancelled',lambda _: False)
        original = os.replace
        def fail_legacy_move(source, destination):
            if Path(source) == legacy:
                raise PermissionError('fixture legacy directory is locked')
            return original(source,destination)
        with mock.patch.object(os, 'replace', fail_legacy_move):
            with self.assertRaises(PermissionError):
                storage.migrate(legacy,target)
        shutil.rmtree(str(target))
        replacement = Store(target)
        keep = replacement.request('synthetic-phone','B','replacement queue')
        with self.assertRaises(QueueError) as error:
            storage.migrate(legacy,target)
        self.assertEqual(error.exception.error,'storage_conflict')
        self.assertTrue(legacy.exists())
        self.assertEqual(old.status(history=True)['tickets'][0]['ticket'],ticket['ticket'])
        self.assertEqual(replacement.status()['tickets'][0]['ticket'],keep['ticket'])

    def test_retry_after_move_failure_preserves_destination_and_retires_legacy(self):
        storage = self.storage()
        legacy, target = self.root / 'old-state', self.root / 'cache/state'
        old = Store(legacy)
        ticket = old.request('synthetic-phone','A','history to preserve')
        old.close(ticket['ticket'],ticket['token'],'cancelled',lambda _: False)
        original = os.replace
        def fail_legacy_move(source, destination):
            if Path(source) == legacy:
                raise PermissionError('fixture legacy directory is locked')
            return original(source,destination)
        with mock.patch.object(os, 'replace', fail_legacy_move):
            with self.assertRaises(PermissionError):
                storage.migrate(legacy,target)
        current = Store(target)
        keep = current.request('synthetic-phone','B','do not overwrite')
        storage.migrate(legacy,target)
        self.assertFalse(legacy.exists())
        self.assertEqual(current.status()['tickets'][0]['ticket'],keep['ticket'])
        self.assertEqual(len(list(target.parent.glob('.legacy-device-queue-state@retired-*'))),1)

    def test_native_removal_during_migration_cannot_recreate_cache(self):
        storage = self.storage()
        legacy = self.root / 'old-state'
        Store(legacy)
        base = self.root / 'codex/plugins/cache/codex-device-queue/codex-device-queue'
        installation = base / '0.3.0'
        installation.mkdir(parents=True)
        target = base / '.device-queue-state@shared'
        original = Store.migrate_legacy
        def concurrent_remove(*args, **kwargs):
            # Windows cannot delete the open migration-lock file, but can remove the version.
            shutil.rmtree(str(installation if os.name == 'nt' else base))
            return original(*args, **kwargs)
        with mock.patch.object(Store,'migrate_legacy',concurrent_remove):
            with self.assertRaises(QueueError) as error:
                storage.migrate(legacy,target,installation=installation)
        self.assertEqual(error.exception.error,'plugin_removed')
        self.assertFalse(installation.exists())
        self.assertFalse(target.exists())
        self.assertTrue(legacy.exists())

    def test_cross_volume_retirement_cleans_legacy_and_preserves_history(self):
        storage = self.storage()
        legacy, target = self.root / 'old-state', self.root / 'cache/state'
        old = Store(legacy)
        ticket = old.request('synthetic-phone','A','cross volume history')
        old.close(ticket['ticket'],ticket['token'],'cancelled',lambda _: False)
        original = os.replace
        def cross_volume(source,destination):
            if Path(source) == legacy:
                raise OSError(errno.EXDEV,'fixture different volume')
            return original(source,destination)
        with mock.patch.object(os,'replace',cross_volume):
            storage.migrate(legacy,target)
        self.assertFalse(legacy.exists())
        self.assertEqual(Store(target).status(history=True)['tickets'][0]['ticket'],ticket['ticket'])

    def test_partial_cross_volume_cleanup_can_resume_without_recreating_legacy_database(self):
        storage = self.storage()
        legacy, target = self.root / 'old-state', self.root / 'cache/state'
        old = Store(legacy)
        ticket = old.request('synthetic-phone','A','keep verified backup')
        old.close(ticket['ticket'],ticket['token'],'cancelled',lambda _: False)
        original = os.replace
        def cross_volume(source,destination):
            if Path(source) == legacy:
                raise OSError(errno.EXDEV,'fixture different volume')
            return original(source,destination)
        def partial_delete(path):
            (Path(path)/'queue.sqlite3').unlink()
            raise PermissionError('fixture locked remaining file')
        with mock.patch.object(os,'replace',cross_volume), mock.patch.object(shutil,'rmtree',partial_delete):
            with self.assertRaises(PermissionError):
                storage.migrate(legacy,target)
        self.assertFalse((legacy/'queue.sqlite3').exists())
        storage.migrate(legacy,target)
        self.assertFalse(legacy.exists())
        self.assertEqual(Store(target).status(history=True)['tickets'][0]['ticket'],ticket['ticket'])

    def test_replaced_installation_cannot_be_reused_by_loaded_store(self):
        installation = self.root / 'cache/0.3.0'
        installation.mkdir(parents=True)
        store = Store(self.root / 'cache/state',installation=installation)
        installation.rename(installation.parent/'previous-version')
        installation.mkdir()
        with self.assertRaises(QueueError) as error:
            store.request('synthetic-phone','A','must not enter replacement installation')
        self.assertEqual(error.exception.error,'plugin_removed')
