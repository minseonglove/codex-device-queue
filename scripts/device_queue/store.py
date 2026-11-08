"""Persistent tickets and atomic per-device grants for one local OS user."""

from contextlib import contextmanager, ExitStack
import hashlib
import errno
import json
import os
from pathlib import Path
import secrets
import re
import shutil
import sqlite3
import time
import uuid

from file_lock import exclusive, publish, sync_directory


class QueueError(Exception):
    def __init__(self, error, message, code=2):
        super().__init__(message)
        self.error = error
        self.code = code


def identifier(value, label):
    if not value or value != value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise QueueError('invalid_input', f'{label} must be a nonempty, stable identifier without surrounding whitespace or control characters')
    return value


class Store:
    def __init__(self, root, installation=None):
        self.installation = Path(installation) if installation is not None else None
        self.installation_generation = self._installation_generation(self.installation)
        self.instance_id = None
        self._check_installation()
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=self.installation is None, exist_ok=True, mode=0o700)
        (self.root / 'locks').mkdir(exist_ok=True, mode=0o700)
        self.database = self.root / 'queue.sqlite3'
        with (self.root / 'schema.lock').open('a') as lock:
            exclusive(lock.fileno())
            db = self.connect(create=True)
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS tickets (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticket TEXT NOT NULL UNIQUE,
                        token TEXT NOT NULL,
                        device TEXT NOT NULL,
                        owner TEXT NOT NULL,
                        purpose TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('queued','granted','released','cancelled','recovered')),
                        managed INTEGER NOT NULL DEFAULT 0,
                        controller_pid INTEGER,
                        created REAL NOT NULL,
                        updated REAL NOT NULL,
                        queue_order INTEGER NOT NULL DEFAULT 9223372036854775807
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS one_grant_per_device
                        ON tickets(device) WHERE state='granted';
                    CREATE UNIQUE INDEX IF NOT EXISTS one_active_owner_device
                        ON tickets(device,owner) WHERE state IN ('queued','granted');
                    CREATE TABLE IF NOT EXISTS operations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticket TEXT NOT NULL REFERENCES tickets(ticket),
                        pgid INTEGER NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('running','finished')),
                        started REAL NOT NULL,
                        ended REAL
                    );
                    CREATE INDEX IF NOT EXISTS active_operations ON operations(ticket,state);
                ''')
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS queue_identity (id INTEGER PRIMARY KEY CHECK(id=1), instance TEXT NOT NULL)')
                db.execute('INSERT OR IGNORE INTO queue_identity VALUES(1,?)', (uuid.uuid4().hex,))
                if 'queue_order' not in {r['name'] for r in db.execute('PRAGMA table_info(tickets)')}:
                    db.execute('ALTER TABLE tickets ADD COLUMN queue_order INTEGER NOT NULL DEFAULT 9223372036854775807')
                    db.execute('UPDATE tickets SET queue_order=seq')
                db.execute('CREATE INDEX IF NOT EXISTS device_queue_order ON tickets(device,state,queue_order,seq)')
                # Old clients omit queue_order and grant by seq; fence both paths in the shared database.
                db.execute('''CREATE TRIGGER IF NOT EXISTS append_legacy_request
                    AFTER INSERT ON tickets
                    WHEN NEW.state='queued' AND NEW.queue_order=9223372036854775807
                    BEGIN
                        UPDATE tickets SET queue_order=(
                            SELECT coalesce(max(queue_order),-1)+1 FROM tickets
                            WHERE device=NEW.device AND state='queued' AND ticket<>NEW.ticket
                        ) WHERE ticket=NEW.ticket;
                    END''')
                db.execute('''CREATE TRIGGER IF NOT EXISTS grant_only_queue_head
                    BEFORE UPDATE OF state ON tickets
                    WHEN OLD.state='queued' AND NEW.state='granted' AND EXISTS (
                        SELECT 1 FROM tickets WHERE device=OLD.device AND (
                            state='granted' OR (state='queued' AND (
                                queue_order<OLD.queue_order OR
                                (queue_order=OLD.queue_order AND seq<OLD.seq)
                            ))
                        )
                    )
                    BEGIN
                        SELECT RAISE(IGNORE);
                    END''')
                db.commit()
                self.instance_id = db.execute('SELECT instance FROM queue_identity WHERE id=1').fetchone()[0]
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _installation_generation(path):
        if path is None:
            return None
        if not Path(path).is_dir():
            raise QueueError('plugin_removed', 'The plugin installation was removed; start a new session after reinstalling')
        stat = Path(path).stat()
        return stat.st_dev, stat.st_ino

    def _check_installation(self):
        if self._installation_generation(self.installation) != self.installation_generation:
            raise QueueError('plugin_removed', 'The plugin installation was replaced; start a new session')

    def connect(self, create=False):
        self._check_installation()
        if not create and not self.database.is_file():
            raise QueueError('queue_removed', 'The queue database was removed; this client cannot recreate it')
        target = str(self.database) if create else self.database.as_uri() + '?mode=rw'
        db = sqlite3.connect(target, uri=not create, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        if not create:
            try:
                row = db.execute('SELECT instance FROM queue_identity WHERE id=1').fetchone()
                if not row or row[0] != self.instance_id:
                    raise QueueError('queue_removed', 'The queue was replaced; this client cannot reuse its old ownership')
            except BaseException:
                db.close()
                raise
        return db

    @staticmethod
    def _validate_legacy_files(root):
        if root.is_symlink() or not root.is_dir():
            raise QueueError('unexpected_state', 'Legacy queue storage must be a regular directory')
        allowed = {'queue.sqlite3', 'queue.sqlite3-wal', 'queue.sqlite3-shm',
                   'queue.sqlite3-journal', 'schema.lock', 'locks'}
        for path in root.iterdir():
            if path.is_symlink() or path.name not in allowed:
                raise QueueError('unexpected_state', 'Unexpected content in legacy queue storage; nothing will be deleted')
            if path.name == 'locks':
                if not path.is_dir():
                    raise QueueError('unexpected_state', 'Legacy locks path is not a directory')
                for child in path.iterdir():
                    if child.is_symlink() or not child.is_file() or not re.fullmatch(r'[0-9a-f]{64}\.lock', child.name):
                        raise QueueError('unexpected_state', 'Unexpected content in legacy locks; nothing will be deleted')
            elif not path.is_file():
                raise QueueError('unexpected_state', 'Unexpected directory in legacy queue storage')

    @classmethod
    def migrate_legacy(cls, legacy, target, group_alive, installation=None, installation_generation=None):
        generation = cls._installation_generation(installation) if installation_generation is None else installation_generation
        def check_installation():
            if cls._installation_generation(installation) != generation:
                raise QueueError('plugin_removed', 'The plugin was replaced during queue storage migration')
        check_installation()
        cls._validate_legacy_files(legacy)
        receipt = target / '.legacy-migration.json'
        if receipt.exists():
            with receipt.open(encoding='utf-8') as handle:
                info = json.load(handle)
            if (not isinstance(info, dict) or info.get('source') != str(legacy.resolve())
                    or not re.fullmatch(r'[0-9a-f]{32}', info.get('instance', ''))):
                raise QueueError('storage_conflict', 'Legacy migration receipt does not match this state directory')
            cls._retire_legacy(legacy, target, info['instance'], check_installation)
            return
        old = cls(legacy)
        marker = 'queue_storage_migration'
        with ExitStack() as locks:
            schema = locks.enter_context((legacy / 'schema.lock').open('a+b'))
            exclusive(schema.fileno())
            with old.transaction() as db:
                migrated = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (marker,)).fetchone()
                if migrated:
                    destination = db.execute('SELECT target FROM queue_storage_migration').fetchone()
                    if not destination or destination[0] != str(target.resolve()):
                        raise QueueError('storage_conflict', 'Legacy queue migration targets another plugin installation')
                else:
                    if (target / 'queue.sqlite3').exists():
                        raise QueueError('storage_conflict', 'Both legacy and cache queues exist; refusing to merge or overwrite them')
                    if db.execute("SELECT 1 FROM tickets WHERE state IN ('queued','granted') LIMIT 1").fetchone():
                        raise QueueError('device_busy', 'Finish legacy device work and waiting requests before updating queue storage', 4)
                    if any(group_alive(row[0]) for row in db.execute("SELECT pgid FROM operations WHERE state='running'")):
                        raise QueueError('device_busy', 'Legacy device subprocesses are still active', 4)
                for path in (legacy / 'locks').iterdir():
                    handle = locks.enter_context(path.open('r+b'))
                    try:
                        exclusive(handle.fileno(), blocking=False)
                    except BlockingIOError:
                        raise QueueError('device_busy', 'A legacy device lock is still held', 4) from None
                if not migrated:
                    db.execute('CREATE TABLE queue_storage_migration (target TEXT NOT NULL)')
                    db.execute('INSERT INTO queue_storage_migration VALUES(?)', (str(target.resolve()),))
                    for table in ('tickets', 'operations'):
                        for action in ('INSERT', 'UPDATE', 'DELETE'):
                            db.execute("CREATE TRIGGER queue_migrated_%s_%s BEFORE %s ON %s BEGIN SELECT RAISE(ABORT,'Queue storage migrated; restart with the updated plugin'); END" % (table, action.lower(), action, table))
        # Never recreate the cache parent if native removal has already deleted it.
        check_installation()
        target.mkdir(exist_ok=True, mode=0o700)
        sync_directory(target.parent)
        destination = target / 'queue.sqlite3'
        if not destination.exists():
            temporary = target / ('.migration-' + uuid.uuid4().hex)
            try:
                source_db = sqlite3.connect(old.database.as_uri() + '?mode=ro', uri=True)
                try:
                    check_installation()
                    copy = sqlite3.connect(str(temporary))
                    try:
                        source_db.backup(copy)
                        for table in ('tickets', 'operations'):
                            for action in ('insert', 'update', 'delete'):
                                copy.execute('DROP TRIGGER queue_migrated_%s_%s' % (table, action))
                        copy.execute('DROP TABLE queue_storage_migration')
                        copy.commit()
                        # A renamed WAL database may need writable sidecar initialization.
                        # Publish a standalone backup; normal Store startup enables WAL again.
                        copy.execute('PRAGMA journal_mode=DELETE').fetchone()
                    finally:
                        copy.close()
                finally:
                    source_db.close()
                publish(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
        cls._verify_migration_destination(target, old.instance_id)
        check_installation()
        temporary = target / ('.receipt-' + uuid.uuid4().hex)
        try:
            with temporary.open('w', encoding='utf-8') as handle:
                json.dump({'source': str(legacy.resolve()), 'instance': old.instance_id}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            publish(temporary, receipt)
        finally:
            if temporary.exists():
                temporary.unlink()
        cls._retire_legacy(legacy, target, old.instance_id, check_installation)

    @staticmethod
    def _verify_migration_destination(target, instance):
        probe = sqlite3.connect((target / 'queue.sqlite3').as_uri() + '?mode=ro', uri=True)
        try:
            identity = probe.execute('SELECT instance FROM queue_identity WHERE id=1').fetchone()
            if not identity or identity[0] != instance:
                raise QueueError('storage_conflict', 'The migration destination was replaced; legacy history will be preserved')
        finally:
            probe.close()

    @classmethod
    def _retire_legacy(cls, legacy, target, instance, check_installation):
        cls._verify_migration_destination(target, instance)
        cls._validate_legacy_files(legacy)
        # Repeat publication barriers after a previous attempt's I/O failure.
        for path in (target / 'queue.sqlite3', target / '.legacy-migration.json'):
            with path.open('r+b') as handle:
                os.fsync(handle.fileno())
        sync_directory(target)
        sync_directory(target.parent)
        database = legacy / 'queue.sqlite3'
        if database.exists():
            source = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            try:
                identity = source.execute('SELECT instance FROM queue_identity WHERE id=1').fetchone()
                if not identity or identity[0] != instance:
                    raise QueueError('storage_conflict', 'Legacy storage was replaced; it will not be deleted')
            finally:
                source.close()
        with ExitStack() as held:
            paths = [legacy / 'schema.lock']
            if (legacy / 'locks').is_dir():
                paths.extend((legacy / 'locks').iterdir())
            for path in paths:
                if path.exists():
                    handle = held.enter_context(path.open('r+b'))
                    try:
                        exclusive(handle.fileno(), blocking=False)
                    except BlockingIOError:
                        raise QueueError('device_busy', 'A legacy state lock is still held', 4) from None
        check_installation()
        retired = target.parent / ('.legacy-device-queue-state@retired-' + instance)
        if retired.exists():
            raise QueueError('storage_conflict', 'Retired legacy storage already exists; refusing to replace it')
        # An atomic move leaves no partially deleted legacy database on cleanup failure.
        # Retired data is fenced and remains inside the native uninstall boundary.
        try:
            os.replace(str(legacy), str(retired))
        except OSError as error:
            if error.errno != errno.EXDEV and getattr(error, 'winerror', None) != 17:
                raise
            # A verified receipt permits resuming even if a partial deletion removed the DB.
            # Cache and legacy home may reside on different Windows drives or volumes.
            check_installation()
            shutil.rmtree(str(legacy))

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def lookup(self, db, ticket, token=None, owner=None):
        row = db.execute('SELECT * FROM tickets WHERE ticket=?', (ticket,)).fetchone()
        if row is None:
            raise QueueError('not_found', 'Ticket not found')
        if token is not None and not secrets.compare_digest(row['token'].encode(), token.encode('utf-8', errors='surrogatepass')):
            raise QueueError('unauthorized', 'Invalid ownership token')
        if owner is not None and row['owner'] != owner:
            raise QueueError('unauthorized', 'The request belongs to a different session')
        return row

    def owned_requests(self, owner, device=None, ticket=None):
        identifier(owner, 'Session owner')
        if device is not None:
            identifier(device, 'Device ID')
        with self.transaction() as db:
            if ticket is not None:
                row = self.lookup(db, ticket, owner=owner)
                if device is not None and row['device'] != device:
                    raise QueueError('target_mismatch', 'The selected ticket belongs to a different device')
                return [self.view(db, row)]
            rows = db.execute("SELECT * FROM tickets WHERE owner=? AND state IN ('queued','granted') ORDER BY device,seq", (owner,)).fetchall()
            return [self.view(db, row) for row in rows if device is None or row['device'] == device]

    def close_owned(self, ticket, owner, state, group_alive):
        identifier(owner, 'Session owner')
        with self.transaction() as db:
            token = self.lookup(db, ticket, owner=owner)['token']
        # Keep capability handling internal and recheck ownership in the mutation transaction.
        return self.close(ticket, token, state, group_alive, owner=owner)

    def view(self, db, row, secret=False):
        result = {k: row[k] for k in ['ticket', 'device', 'owner', 'purpose', 'state', 'managed', 'created', 'updated']}
        if row['state'] == 'granted':
            result['position'] = 1
        elif row['state'] == 'queued':
            result['position'] = 1 + db.execute("SELECT count(*) FROM tickets WHERE device=? AND (state='granted' OR (state='queued' AND (queue_order<? OR (queue_order=? AND seq<?))))", (row['device'], row['queue_order'], row['queue_order'], row['seq'])).fetchone()[0]
        else:
            result['position'] = 0
        result['operations'] = [r['pgid'] for r in db.execute("SELECT pgid FROM operations WHERE ticket=? AND state='running'", (row['ticket'],))]
        if secret:
            result['token'] = row['token']
        return result

    def head(self, db, device):
        return db.execute("SELECT ticket FROM tickets WHERE device=? AND state IN ('queued','granted') ORDER BY (state='granted') DESC,queue_order,seq LIMIT 1", (device,)).fetchone()

    def request(self, device, owner, purpose, managed=False, request_id=None, front=False):
        identifier(device, 'Device ID')
        identifier(owner, 'Session owner')
        if not purpose.strip():
            raise QueueError('invalid_input', 'A purpose is required')
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tickets WHERE device=? AND owner=? AND state IN ('queued','granted')", (device, owner)).fetchone()
            created = row is None
            if created:
                now = time.time()
                ticket, token = request_id or uuid.uuid4().hex, 'dq_' + secrets.token_urlsafe(32)
                bounds = db.execute("SELECT min(queue_order),max(queue_order) FROM tickets WHERE device=? AND state='queued'", (device,)).fetchone()
                boundary = bounds[0 if front else 1]
                order = 0 if boundary is None else boundary + (-1 if front else 1)
                db.execute("INSERT INTO tickets(ticket,token,device,owner,purpose,state,managed,controller_pid,created,updated,queue_order) VALUES(?,?,?,?,?,'queued',?,?,?,?,?)", (ticket, token, device, owner, purpose, int(managed), os.getpid() if managed else None, now, now, order))
                row = self.lookup(db, ticket)
            result = self.view(db, row, secret=True)
            result['created'] = created
            return result

    def get(self, ticket, token):
        with self.transaction() as db:
            return dict(self.lookup(db, ticket, token))

    @contextmanager
    def device_lock(self, device):
        digest = hashlib.sha256(device.encode()).hexdigest()
        fd = os.open(self.root / 'locks' / (digest + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                exclusive(fd, blocking=False)
            except BlockingIOError:
                raise QueueError('device_busy', 'A managed device command still holds the device lock', 4) from None
            yield fd
        finally:
            # Closing only our descriptor preserves the lock inherited by live workers.
            os.close(fd)

    def acquire(self, ticket, token):
        row = self.get(ticket, token)
        if row['state'] not in ('queued', 'granted'):
            raise QueueError('closed_ticket', 'This request is no longer active')
        with self.transaction() as db:
            row = self.lookup(db, ticket, token)
            head = self.head(db, row['device'])
            if row['state'] not in ('queued', 'granted'):
                raise QueueError('closed_ticket', 'This request is no longer active')
            if not head or head['ticket'] != ticket:
                return self.view(db, row)
        with self.device_lock(row['device']):
            with self.transaction() as db:
                row = self.lookup(db, ticket, token)
                if row['state'] not in ('queued', 'granted'):
                    raise QueueError('closed_ticket', 'This request is no longer active')
                head = self.head(db, row['device'])
                if row['state'] == 'queued' and head and head['ticket'] == ticket:
                    db.execute("UPDATE tickets SET state='granted',updated=? WHERE ticket=?", (time.time(), ticket))
                return self.view(db, self.lookup(db, ticket, token))

    def status(self, device=None, history=False):
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM tickets ORDER BY device,(state='granted') DESC,(state='queued') DESC,queue_order,seq").fetchall()
            return {'state_dir': str(self.root), 'tickets': [self.view(db, r) for r in rows if (device is None or r['device'] == device) and (history or r['state'] in ('queued', 'granted'))]}

    def active_groups(self, db, ticket):
        return [r['pgid'] for r in db.execute("SELECT pgid FROM operations WHERE ticket=? AND state='running'", (ticket,))]

    def close(self, ticket, token, state, group_alive, owner=None):
        row = self.get(ticket, token)
        if state == 'cancelled':
            with self.transaction() as db:
                row = self.lookup(db, ticket, token, owner=owner)
                if row['state'] == 'granted':
                    raise QueueError('already_granted', 'Use release after stopping device work')
                if row['state'] == 'queued':
                    db.execute("UPDATE tickets SET state='cancelled',updated=? WHERE ticket=?", (time.time(), ticket))
                return self.view(db, self.lookup(db, ticket, token))
        with self.device_lock(row['device']):
            with self.transaction() as db:
                row = self.lookup(db, ticket, token, owner=owner)
                if row['state'] in ('released', 'cancelled', 'recovered'):
                    return self.view(db, row)
                if state == 'cancelled' and row['state'] == 'granted':
                    raise QueueError('already_granted', 'Use release after stopping device work')
                if state == 'released' and row['state'] != 'granted':
                    raise QueueError('not_granted', 'Cancel a queued ticket instead of releasing it')
                if any(group_alive(g) for g in self.active_groups(db, ticket)):
                    raise QueueError('device_busy', 'A registered process group is still active', 4)
                db.execute("UPDATE operations SET state='finished',ended=? WHERE ticket=? AND state='running'", (time.time(), ticket))
                db.execute('UPDATE tickets SET state=?,updated=? WHERE ticket=?', (state, time.time(), ticket))
                return self.view(db, self.lookup(db, ticket, token))
