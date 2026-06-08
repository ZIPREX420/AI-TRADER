"""Direct tests for helpers introduced by the codebase-wide refactor pass.

These lock in the behaviour-preserving extractions so future edits can't
silently change them:
  * runtime/app.py     -> Application._aclose_quietly  (best-effort shutdown close)
  * foundation/cli.py  -> _bootstrap_ctx               (config-from-context helper)
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ---- Application._aclose_quietly ----


async def test_aclose_quietly_none_is_noop(app_config) -> None:
    from solalpha.runtime.app import Application

    app = Application(app_config)
    await app._aclose_quietly("nothing", None)  # must not raise


async def test_aclose_quietly_calls_aclose(app_config) -> None:
    from solalpha.runtime.app import Application

    class _Client:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    app = Application(app_config)
    client = _Client()
    await app._aclose_quietly("client", client)  # type: ignore[arg-type]
    assert client.closed is True


async def test_aclose_quietly_swallows_errors(app_config) -> None:
    from solalpha.runtime.app import Application

    class _Boom:
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    app = Application(app_config)
    # A failing close must not propagate -- the other clients + store close
    # + final snapshot still have to run.
    await app._aclose_quietly("boom", _Boom())  # type: ignore[arg-type]


# ---- cli._bootstrap_ctx ----


class _Ctx:
    def __init__(self, config_dir, profile) -> None:
        self.obj = {"config_dir": config_dir, "profile": profile}


def test_bootstrap_ctx_passes_context_values_through(monkeypatch) -> None:
    import solalpha.foundation.cli as cli

    seen: dict[str, object] = {}

    def fake_bootstrap(config_dir, profile):
        seen["config_dir"] = config_dir
        seen["profile"] = profile
        return "CFG"

    monkeypatch.setattr(cli, "_bootstrap", fake_bootstrap)
    result = cli._bootstrap_ctx(_Ctx("/cfg", "explicit"))
    assert result == "CFG"
    assert seen == {"config_dir": "/cfg", "profile": "explicit"}


def test_bootstrap_ctx_uses_default_profile_when_ctx_profile_absent(monkeypatch) -> None:
    import solalpha.foundation.cli as cli

    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "_bootstrap", lambda cd, p: seen.update(profile=p))
    cli._bootstrap_ctx(_Ctx(None, None), "research")
    assert seen["profile"] == "research"


def test_bootstrap_ctx_ctx_profile_beats_default(monkeypatch) -> None:
    import solalpha.foundation.cli as cli

    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "_bootstrap", lambda cd, p: seen.update(profile=p))
    cli._bootstrap_ctx(_Ctx(None, "explicit"), "research")
    assert seen["profile"] == "explicit"
