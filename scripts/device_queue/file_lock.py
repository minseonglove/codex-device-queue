"""Exclusive local-file locks; descriptor close releases the caller's lock."""
import os
from pathlib import Path


def exclusive(fd, blocking=True):
    if os.name == 'nt':
        from windows_backend import lock_file
        lock_file(fd, blocking)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def sync_directory(path):
    if os.name != 'nt':
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish(source, destination):
    with Path(source).open('r+b') as handle:
        os.fsync(handle.fileno())
    if os.name == 'nt':
        from windows_backend import publish_file
        publish_file(source, destination)
    else:
        os.replace(str(source), str(destination))
        sync_directory(Path(destination).parent)
