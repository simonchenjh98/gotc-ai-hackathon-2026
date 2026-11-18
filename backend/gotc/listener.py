"""Bind the local Unix socket used by one Nginx upstream."""
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import stat


@contextmanager
def unix_listener(address):
    import fcntl

    path = Path(address)
    if not path.is_absolute() or path.parent.stat().st_mode & stat.S_IWOTH:
        raise ValueError("Use an absolute socket path inside a private runtime directory")
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as lock, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            old = path.lstat()
        except FileNotFoundError:
            old = None
        if old is not None:
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid():
                raise RuntimeError("Service socket path is occupied")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.3)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    path.unlink()
                else:
                    raise RuntimeError("Another service is listening on the configured socket")
        listener.bind(str(path))
        created = path.lstat()
        try:
            path.chmod(0o660)
            listener.listen(128)
            yield listener
        finally:
            try:
                if os.path.samestat(created, path.lstat()):
                    path.unlink()
            except FileNotFoundError:
                pass
