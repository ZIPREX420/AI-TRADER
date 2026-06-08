"""Config: layered loading, hard ceilings, live-trading env gate."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from solalpha.foundation.config import AppConfig, load_config
from solalpha.foundation.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def test_default_load(tmp_path: Path) -> None:
    cfg = AppConfig.model_validate({})
    assert cfg.mode == "PAPER"
    assert cfg.live_trading is False
    assert cfg.risk.max_slippage_bps == 150


def test_hard_ceiling_slippage_refused() -> None:
    with pytest.raises(ConfigError, match="hard ceiling"):
        AppConfig.model_validate(
            {"risk": {"max_slippage_bps": 999, "hard_slippage_ceiling_bps": 300}}
        )


def test_hard_ceiling_open_positions_refused() -> None:
    with pytest.raises(ConfigError, match="ceiling"):
        AppConfig.model_validate(
            {"risk": {"max_open_positions": 99, "max_open_positions_ceiling": 16}}
        )


def test_hard_ceiling_price_impact_refused() -> None:
    with pytest.raises(ConfigError, match="ceiling"):
        AppConfig.model_validate(
            {"risk": {"max_price_impact_pct": 0.99, "max_price_impact_ceiling_pct": 0.05}}
        )


def test_detector_weights_must_sum_to_one() -> None:
    with pytest.raises(ConfigError, match="weights must sum"):
        AppConfig.model_validate(
            {"signals": {"weights": {"prepump": 0.1, "cluster": 0.1, "flow_anomaly": 0.1}}}
        )


def test_live_requires_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOLALPHA_LIVE_TRADING", raising=False)
    monkeypatch.delenv("SOLANA_LIVE_TRADING", raising=False)
    with pytest.raises(ConfigError, match="env flag"):
        AppConfig.model_validate({"mode": "LIVE"})


def test_live_config_demotes_to_paper_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOLALPHA_LIVE_TRADING", raising=False)
    monkeypatch.delenv("SOLANA_LIVE_TRADING", raising=False)
    cfg = AppConfig.model_validate({"live_trading": True})
    # The validator demotes to PAPER (no exception).
    assert cfg.mode == "PAPER"
    assert cfg.is_live_eligible() is False


def test_live_with_env_flag_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLALPHA_LIVE_TRADING", "1")
    cfg = AppConfig.model_validate({"mode": "LIVE", "live_trading": True})
    assert cfg.mode == "LIVE"
    assert cfg.is_live_eligible() is True


def test_load_config_layered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "default.yaml").write_text("risk:\n  per_trade_usd_cap: 250.0\n")
    (cfg_dir / "paper.yaml").write_text("risk:\n  per_trade_usd_cap: 100.0\n")
    monkeypatch.delenv("SOLALPHA_PROFILE", raising=False)
    cfg = load_config(config_dir=cfg_dir, profile="paper")
    assert cfg.risk.per_trade_usd_cap == 100.0


def test_research_allow_live_writes_always_false() -> None:
    with pytest.raises(ConfigError, match="allow_live_writes"):
        AppConfig.model_validate({"research": {"allow_live_writes": True}})


def test_rpc_urls_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SOLALPHA_RPC_URLS",
        "https://a.example/, https://b.example/",
    )
    cfg = load_config(config_dir=os.devnull and None)  # type: ignore[arg-type]
    assert "https://a.example/" in cfg.rpc.urls
    assert "https://b.example/" in cfg.rpc.urls
