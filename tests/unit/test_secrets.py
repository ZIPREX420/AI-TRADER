"""KeypairLoader: format parsing, caching, missing/invalid handling, posix perms.

Covers `foundation/secrets.py`, which loads the Solana signing keypair for live
execution. Previously exercised only indirectly; these pin the parse/validation
and the posix permission guard directly.
"""

from __future__ import annotations

import json
import os

import pytest

from solalpha.foundation.errors import ConfigError
from solalpha.foundation.secrets import KeypairLoader

pytestmark = pytest.mark.unit

_SECRET = bytes(range(64))  # a valid 64-byte secret key


def _write_json_keypair(path, secret: bytes = _SECRET, mode: int = 0o600) -> None:
    path.write_text(json.dumps(list(secret)))
    if os.name == "posix":
        path.chmod(mode)


def test_unconfigured_when_path_none() -> None:
    loader = KeypairLoader(None)
    assert loader.path is None
    assert loader.is_configured() is False


def test_unconfigured_when_file_missing(tmp_path) -> None:
    loader = KeypairLoader(str(tmp_path / "absent.json"))
    assert loader.is_configured() is False


def test_load_bytes_from_json_array(tmp_path) -> None:
    kp = tmp_path / "kp.json"
    _write_json_keypair(kp)
    loader = KeypairLoader(str(kp))
    assert loader.is_configured() is True
    assert loader.load_bytes() == _SECRET


def test_load_bytes_is_cached(tmp_path) -> None:
    kp = tmp_path / "kp.json"
    _write_json_keypair(kp)
    loader = KeypairLoader(str(kp))
    first = loader.load_bytes()
    kp.unlink()  # cache must survive the file going away
    assert loader.load_bytes() == first


def test_raw_64_byte_file_fallback(tmp_path) -> None:
    # 64 bytes, no leading/trailing whitespace, does not start with b"["
    secret = bytes(range(33, 97))
    kp = tmp_path / "raw.key"
    kp.write_bytes(secret)
    if os.name == "posix":
        kp.chmod(0o600)
    assert KeypairLoader(str(kp)).load_bytes() == secret


def test_load_bytes_raises_when_path_none() -> None:
    with pytest.raises(ConfigError, match="not set"):
        KeypairLoader(None).load_bytes()


def test_load_bytes_raises_when_missing(tmp_path) -> None:
    loader = KeypairLoader(str(tmp_path / "nope.json"))
    with pytest.raises(ConfigError, match="not found"):
        loader.load_bytes()


def test_malformed_json_raises(tmp_path) -> None:
    kp = tmp_path / "bad.json"
    kp.write_text("[1, 2, 3,]")  # bracketed but invalid (trailing comma)
    if os.name == "posix":
        kp.chmod(0o600)
    with pytest.raises(ConfigError, match="malformed"):
        KeypairLoader(str(kp)).load_bytes()


def test_json_not_list_of_ints_raises(tmp_path) -> None:
    kp = tmp_path / "bad2.json"
    kp.write_text(json.dumps(["a", "b"]))
    if os.name == "posix":
        kp.chmod(0o600)
    with pytest.raises(ConfigError, match="list of ints"):
        KeypairLoader(str(kp)).load_bytes()


def test_wrong_length_raises(tmp_path) -> None:
    kp = tmp_path / "short.json"
    _write_json_keypair(kp, bytes(range(32)))  # 32 != 64
    with pytest.raises(ConfigError, match="64-byte"):
        KeypairLoader(str(kp)).load_bytes()


@pytest.mark.skipif(os.name != "posix", reason="file-permission check is posix-only")
def test_rejects_group_world_readable(tmp_path) -> None:
    kp = tmp_path / "loose.json"
    kp.write_text(json.dumps(list(_SECRET)))
    kp.chmod(0o644)  # group/world readable -> rejected
    with pytest.raises(ConfigError, match="permissions"):
        KeypairLoader(str(kp)).load_bytes()


@pytest.mark.skipif(os.name != "posix", reason="file-permission check is posix-only")
def test_accepts_owner_only_perms(tmp_path) -> None:
    kp = tmp_path / "tight.json"
    kp.write_text(json.dumps(list(_SECRET)))
    kp.chmod(0o600)
    assert KeypairLoader(str(kp)).load_bytes() == _SECRET
