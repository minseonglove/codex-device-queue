"""Locate version-common queue state inside Codex's native removal boundary."""
import os
from pathlib import Path

from file_lock import exclusive
from store import Store

PLUGIN_NAME = 'codex-device-queue'
MARKETPLACE = 'codex-device-queue'
STATE_NAME = '.device-queue-state@shared'


def installed_root(plugin_root):
    root = Path(plugin_root).resolve()
    base = root.parent
    if (base.name == PLUGIN_NAME and base.parent.parent.name == 'cache'
            and base.parent.parent.parent.name == 'plugins'):
        return root
    return None


def default_state(plugin_root=None):
    root = Path(plugin_root) if plugin_root is not None else Path(__file__).resolve().parents[2]
    installed = installed_root(root)
    if installed is not None:
        base = installed.parent
    else:
        codex_home = Path(os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')).expanduser().resolve()
        base = codex_home / 'plugins/cache' / MARKETPLACE / PLUGIN_NAME
    # '@' is not a valid Codex version segment: upgrades/discovery ignore this directory.
    return base / STATE_NAME


def migrate(legacy, target, installation=None):
    legacy, target = Path(legacy), Path(target)
    if not legacy.exists() and not legacy.is_symlink():
        return
    if installation is not None:
        generation = Store._installation_generation(installation)
    else:
        generation = None
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (target.parent / '.queue-migration.lock').open('a+b') as lock:
        exclusive(lock.fileno())
        if not legacy.exists() and not legacy.is_symlink():
            return
        from runner import group_alive
        Store.migrate_legacy(legacy, target, group_alive, installation=installation,
                             installation_generation=generation)
