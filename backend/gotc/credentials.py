"""Read named systemd credentials with private-directory fallbacks for local events."""
import hmac
import json
import os
from collections import ChainMap
from functools import cached_property
from pathlib import Path
import re
import shlex
import stat
import tempfile


CREDENTIAL_NAMES = frozenset({
    "api-key-pepper", "roadshow-claim-key", "gateway.json", "repositories.env",
    "shared-login-codes.json", "finals-access-code.txt",
})


class Credentials:
    def __init__(self, directory):
        self.directory = Path(directory)
        runtime = os.environ.get("CREDENTIALS_DIRECTORY")
        self.systemd = Path(runtime) if runtime else None

    @staticmethod
    def _read(directory, name):
        if name not in CREDENTIAL_NAMES:
            raise ValueError("Unknown credential name")
        if directory is None:
            return None
        path = directory / name
        try:
            if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
                raise OSError("Invalid credential source")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return None
        except OSError:
            raise RuntimeError(f"Cannot open credential {name}") from None
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(f"Invalid credential file {name}")
            data = stream.read(1_048_577)
        if len(data) > 1_048_576:
            raise ValueError(f"Credential {name} exceeds the size limit")
        return data

    def read(self, name):
        data = self._read(self.systemd, name)
        return data if data is not None else self._read(self.directory, name)

    def read_json(self, name, default=None):
        data = self.read(name)
        if data is None:
            return default
        try:
            return json.loads(data)
        except (ValueError, RecursionError):
            # JSONDecodeError retains the complete credential document.
            pass
        raise ValueError(f"Invalid JSON in {name}")

    def repository_settings(self):
        # A credential file overrides explicitly exported local development settings.
        return ChainMap(self._repository_file, os.environ)

    @cached_property
    def _repository_file(self):
        result = {}
        data = self.read("repositories.env")
        if data is None:
            return result
        try:
            for line in data.decode().splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                key, sep, raw = line.partition("=")
                key = key.strip()
                if not sep or not re.fullmatch(r"GOTC_REPO_[A-Z0-9_]+", key):
                    raise ValueError
                parts = shlex.split(raw, comments=True)
                if len(parts) > 1:
                    raise ValueError
                result[key] = parts[0] if parts else ""
        except (ValueError, UnicodeError):
            raise ValueError("Invalid repositories.env assignment") from None
        return result

    def stable_key(self, name, generate, *, can_generate):
        """Keep existing keys for restored databases. Publish a complete key atomically."""
        def checked(data):
            if data is None:
                return None
            if name == "api-key-pepper":
                try:
                    decoded = bytes.fromhex(data.decode("ascii"))
                except ValueError:
                    decoded = b""
                if len(decoded) == 32:
                    return decoded.hex().encode("ascii")
            elif name == "roadshow-claim-key" and len(data) == 32:
                return data
            raise ValueError(f"Invalid key material for {name}")

        provisioned = checked(self._read(self.systemd, name))
        stored = checked(self._read(self.directory, name))
        if stored is None:
            if provisioned is None and not can_generate():
                label = name.replace("-", " ")
                raise RuntimeError(f"The original {label} is missing; restore it with the database")
            value = provisioned if provisioned is not None else checked(generate())
            with tempfile.NamedTemporaryFile(dir=self.directory) as temporary:
                temporary.write(value)
                temporary.flush()
                os.fsync(temporary.fileno())
                try:
                    os.link(temporary.name, self.directory / name)
                except FileExistsError:
                    pass
            stored = checked(self._read(self.directory, name))
        if stored is None or (provisioned is not None and not hmac.compare_digest(stored, provisioned)):
            raise RuntimeError(f"Provisioned {name} differs from the event key")
        return stored
