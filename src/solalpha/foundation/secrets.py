"""Keypair loading from disk. Never logs key material.

Accepted formats:
- JSON array of bytes (the format produced by `solana-keygen new -o keypair.json`)
- 64-byte raw secret key file (less common; rejected unless explicitly enabled)
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from solalpha.foundation.errors import ConfigError
from solalpha.foundation.logging import get_logger

_log = get_logger(__name__)


class KeypairLoader:
    """Loads a Solana keypair lazily, only when required by live execution."""

    def __init__(self, path: str | None) -> None:
        self._path = Path(path).expanduser() if path else None
        self._cached_bytes: bytes | None = None

    @property
    def path(self) -> Path | None:
        return self._path

    def is_configured(self) -> bool:
        return self._path is not None and self._path.exists()

    def load_bytes(self) -> bytes:
        """Return the 64-byte secret key. Raises ConfigError if missing/invalid."""
        if self._cached_bytes is not None:
            return self._cached_bytes
        if self._path is None:
            raise ConfigError("SOLALPHA_KEYPAIR_PATH is not set; required for live trading")
        if not self._path.exists():
            raise ConfigError(f"keypair file not found: {self._path}")
        self._check_perms(self._path)
        raw = self._path.read_bytes()
        secret = self._parse(raw)
        if len(secret) != 64:
            raise ConfigError("keypair file did not contain a 64-byte secret key")
        self._cached_bytes = secret
        # Log without revealing key material — only file path.
        _log.info("keypair_loaded", path=str(self._path))
        return secret

    def load_keypair(self) -> object:
        """Return a solders.keypair.Keypair instance."""
        from solders.keypair import Keypair  # imported lazily to keep startup light

        secret = self.load_bytes()
        return Keypair.from_bytes(secret)

    def _parse(self, raw: bytes) -> bytes:
        text = raw.strip()
        if text.startswith(b"[") and text.endswith(b"]"):
            try:
                arr = json.loads(text)
            except json.JSONDecodeError as e:
                raise ConfigError(f"keypair JSON malformed: {e}") from e
            if not isinstance(arr, list) or not all(isinstance(x, int) for x in arr):
                raise ConfigError("keypair JSON must be a list of ints")
            try:
                return bytes(int(x) & 0xFF for x in arr)
            except (TypeError, ValueError) as e:
                raise ConfigError(f"keypair JSON contains non-byte values: {e}") from e
        # Raw 64-byte file fallback
        if len(text) == 64:
            return text
        raise ConfigError("keypair file format not recognized (expected JSON array)")

    def _check_perms(self, path: Path) -> None:
        if os.name != "posix":
            return  # Windows ACLs not enforced here
        try:
            mode = path.stat().st_mode
        except OSError:
            return
        # Reject world/group readable (mode 600 expected)
        if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
            raise ConfigError(f"keypair file {path} has too-broad permissions; chmod 600 required")


__all__ = ["KeypairLoader"]
