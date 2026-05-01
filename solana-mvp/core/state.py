"""Atomic JSON read/write for persistent state."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


class JsonStore:
    """Cached round-trip with atomic writes."""

    def __init__(self, path: str | Path, default: Any):
        self.path = Path(path)
        self._default = default
        self._cache: Any = None

    def load(self) -> Any:
        if self._cache is None:
            self._cache = read_json(self.path, default=self._default)
            if self._cache is None:
                self._cache = self._default
        return self._cache

    def save(self, data: Any | None = None) -> None:
        if data is not None:
            self._cache = data
        atomic_write_json(self.path, self._cache)

    def update(self, fn) -> Any:
        """fn(state) → new_state; atomic save."""
        cur = self.load()
        new = fn(cur)
        if new is not None:
            self._cache = new
        self.save()
        return self._cache
