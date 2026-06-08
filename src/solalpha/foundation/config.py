"""Configuration: layered YAML + env vars + validation.

Order of precedence (last wins):
1. `config/default.yaml`
2. `config/<profile>.yaml` (profile from env `SOLALPHA_PROFILE`)
3. Environment variables (prefix `SOLALPHA_`, double-underscore for nesting:
   `SOLALPHA_RISK__PER_TRADE_USD_CAP=100`)

Hard risk ceilings are enforced by validators; the live mode requires
both `live_trading: true` in YAML AND `SOLALPHA_LIVE_TRADING=1` env.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from solalpha.foundation.errors import ConfigError

ModeStr = Literal["LIVE", "DEGRADED_RPC", "DEGRADED_EXEC", "PAPER", "HALT"]


class LoggingConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    level: Literal["debug", "info", "warning", "error"] = "info"
    format: Literal["json", "console"] = "json"


class PersistenceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    data_dir: Path = Path("./data")
    log_dir: Path = Path("./logs")
    sqlite_filename: str = "solalpha.db"
    parquet_dir: str = "parquet"
    snapshot_dir: str = "snapshots"
    snapshot_interval_s: float = 60.0
    journal_retention_days: int = 14
    parquet_compaction_hour_utc: int = 2

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / self.sqlite_filename

    @property
    def parquet_root(self) -> Path:
        return self.data_dir / self.parquet_dir

    @property
    def snapshot_root(self) -> Path:
        return self.data_dir / self.snapshot_dir


class MetricsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9464


class RpcConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    urls: list[str] = Field(default_factory=list)
    ws_urls: list[str] = Field(default_factory=list)
    request_timeout_s: float = 8.0
    ws_heartbeat_s: float = 15.0
    ws_reconnect_max_s: float = 30.0
    health_quarantine_s: float = 30.0
    health_min_healthy: int = 2
    health_window_s: float = 60.0
    health_min_success_rate: float = 0.6


class JupiterConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    base_url: str = "https://quote-api.jup.ag/v6"
    quote_timeout_s: float = 5.0
    swap_timeout_s: float = 8.0
    max_retries: int = 3
    default_slippage_bps: int = 100


class RaydiumConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool = True
    base_url: str = "https://api-v3.raydium.io"
    request_timeout_s: float = 5.0


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    compute_unit_limit: int = 400_000
    default_priority_fee_lamports: int = 5_000
    bump_priority_fee_lamports: list[int] = Field(default_factory=lambda: [5_000, 25_000, 100_000])
    bump_slippage_bps: list[int] = Field(default_factory=lambda: [0, 50, 100])
    max_attempts: int = 3
    confirmation_timeout_s: float = 30.0
    confirmation_poll_interval_s: float = 1.0
    blockhash_max_age_slots: int = 150


class RiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    max_slippage_bps: int = 150
    hard_slippage_ceiling_bps: int = 300
    per_trade_usd_cap: float = 250.0
    per_trade_pct: float = 0.02
    per_mint_pct_cap: float = 0.05
    max_open_positions: int = 8
    max_open_positions_ceiling: int = 16
    daily_loss_pct: float = 0.05
    loss_streak_max: int = 5
    min_confidence: float = 0.55
    vol_halt_5m_pct: float = 0.08
    vol_halt_duration_s: int = 900
    quarantine_duration_s: int = 86_400
    min_pool_liquidity_usd: float = 25_000.0
    max_price_impact_pct: float = 0.025
    max_price_impact_ceiling_pct: float = 0.05
    smart_wallet_min_age_days: int = 7
    smart_wallet_min_tx: int = 20
    max_inflight_per_mint: int = 1
    starting_equity_usd: float = 1000.0

    @model_validator(mode="after")
    def _enforce_hard_ceilings(self) -> RiskConfig:
        if self.max_slippage_bps > self.hard_slippage_ceiling_bps:
            raise ConfigError(
                f"max_slippage_bps {self.max_slippage_bps} exceeds hard ceiling "
                f"{self.hard_slippage_ceiling_bps}"
            )
        if self.max_open_positions > self.max_open_positions_ceiling:
            raise ConfigError(
                f"max_open_positions {self.max_open_positions} exceeds ceiling "
                f"{self.max_open_positions_ceiling}"
            )
        if self.max_price_impact_pct > self.max_price_impact_ceiling_pct:
            raise ConfigError(
                f"max_price_impact_pct {self.max_price_impact_pct} exceeds ceiling "
                f"{self.max_price_impact_ceiling_pct}"
            )
        if not (0.0 < self.daily_loss_pct < 1.0):
            raise ConfigError("daily_loss_pct must be between 0 and 1 exclusive")
        if not (0.0 <= self.min_confidence <= 1.0):
            raise ConfigError("min_confidence must be in [0, 1]")
        if self.per_trade_pct < 0 or self.per_trade_pct > 1:
            raise ConfigError("per_trade_pct must be in [0, 1]")
        return self


class ModeManagerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    hysteresis_live_to_degraded_rpc_s: float = 10.0
    hysteresis_degraded_rpc_to_live_s: float = 30.0
    hysteresis_live_to_degraded_exec_s: float = 30.0
    hysteresis_degraded_exec_to_live_s: float = 60.0
    hysteresis_to_paper_s: float = 120.0
    paper_to_live_health_s: float = 300.0
    degraded_rpc_confidence_bonus: float = 0.15
    degraded_rpc_size_factor: float = 0.5


class SignalsPrePumpConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    window_s: int = 90
    min_buy_pressure_ratio: float = 1.6
    min_liquidity_slope_pct_per_min: float = 0.05


class SignalsClusterConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    wallets_required: int = 3
    window_s: int = 120
    min_total_buy_usd: float = 1500.0


class SignalsFlowAnomalyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    baseline_window_s: int = 1800
    z_threshold: float = 2.5


class SignalsWeightsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    prepump: float = 0.35
    cluster: float = 0.45
    flow_anomaly: float = 0.20

    @model_validator(mode="after")
    def _check_sum(self) -> SignalsWeightsConfig:
        s = self.prepump + self.cluster + self.flow_anomaly
        if abs(s - 1.0) > 1e-3:
            raise ConfigError(f"detector weights must sum to 1.0, got {s}")
        return self


class SignalsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    prepump: SignalsPrePumpConfig = Field(default_factory=SignalsPrePumpConfig)
    cluster: SignalsClusterConfig = Field(default_factory=SignalsClusterConfig)
    flow_anomaly: SignalsFlowAnomalyConfig = Field(default_factory=SignalsFlowAnomalyConfig)
    weights: SignalsWeightsConfig = Field(default_factory=SignalsWeightsConfig)


class SmartWalletsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    decay_half_life_days: float = 14.0
    min_score_to_subscribe: float = 0.20
    max_subscriptions: int = 200


class ResearchConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    allow_live_writes: bool = False
    walkforward_train_days: int = 14
    walkforward_test_days: int = 3
    min_oos_sharpe: float = 0.5

    @field_validator("allow_live_writes")
    @classmethod
    def _refuse_live_writes(cls, v: bool) -> bool:
        # Defensive: research overlay must NEVER mutate live state.
        if v:
            raise ConfigError("research.allow_live_writes must always be False")
        return v


class KillSwitchConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    file_path: Path = Path("./data/.kill")
    poll_interval_s: float = 1.0


class PaperExecutorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    fee_bps: int = 30
    base_slippage_bps: int = 25
    impact_slippage_per_pct: float = 8.0


class AppConfig(BaseSettings):
    """Top-level config; constructed via `load_config()` only."""

    model_config = SettingsConfigDict(
        env_prefix="SOLALPHA_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    profile: str = "default"
    mode: ModeStr = "PAPER"
    live_trading: bool = False
    keypair_path: SecretStr | None = None

    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    rpc: RpcConfig = Field(default_factory=RpcConfig)
    jupiter: JupiterConfig = Field(default_factory=JupiterConfig)
    raydium: RaydiumConfig = Field(default_factory=RaydiumConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    mode_manager: ModeManagerConfig = Field(default_factory=ModeManagerConfig)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    smart_wallets: SmartWalletsConfig = Field(default_factory=SmartWalletsConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    kill_switch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)
    paper_executor: PaperExecutorConfig = Field(default_factory=PaperExecutorConfig)

    @model_validator(mode="after")
    def _enforce_live_env_flag(self) -> AppConfig:
        # Live trading requires BOTH yaml and env to agree.
        env_flag = (
            os.environ.get("SOLALPHA_LIVE_TRADING") == "1"
            or os.environ.get("SOLANA_LIVE_TRADING") == "1"
        )
        if self.mode == "LIVE" and not env_flag:
            raise ConfigError(
                "live mode requires SOLALPHA_LIVE_TRADING=1 env flag "
                "(or alias SOLANA_LIVE_TRADING=1)"
            )
        if self.live_trading and not env_flag:
            # config asks for live, env hasn't confirmed → demote to PAPER but warn.
            object.__setattr__(self, "mode", "PAPER")
        return self

    def is_live_eligible(self) -> bool:
        return self.live_trading and (
            os.environ.get("SOLALPHA_LIVE_TRADING") == "1"
            or os.environ.get("SOLANA_LIVE_TRADING") == "1"
        )


# ---- Loader ----


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"YAML root in {path} must be a mapping, got {type(data).__name__}")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# Env var -> (dotted config path, transform). split = comma-separated list,
# lower = lowercased string, raw = used verbatim.
_ENV_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("SOLALPHA_RPC_URLS", "rpc.urls", "split"),
    ("SOLALPHA_RPC_WS_URLS", "rpc.ws_urls", "split"),
    ("SOLALPHA_KEYPAIR_PATH", "keypair_path", "raw"),
    ("SOLALPHA_JUPITER_BASE_URL", "jupiter.base_url", "raw"),
    ("SOLALPHA_LOG_LEVEL", "logging.level", "lower"),
    ("SOLALPHA_LOG_FORMAT", "logging.format", "lower"),
)


def _apply_env_overrides(merged: dict[str, Any]) -> None:
    """Map env-only conveniences into ``merged`` before pydantic-settings runs,
    since some keys live outside the nested config structure."""
    for env_var, path, transform in _ENV_OVERRIDES:
        raw = os.environ.get(env_var)
        if not raw:
            continue
        if transform == "split":
            value: Any = [u.strip() for u in raw.split(",") if u.strip()]
        elif transform == "lower":
            value = raw.lower()
        else:
            value = raw
        *parents, leaf = path.split(".")
        target: Any = merged
        for parent in parents:
            target = target.setdefault(parent, {})
        target[leaf] = value


def load_config(
    *,
    config_dir: Path | None = None,
    profile: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Load AppConfig with full layering."""
    cfg_dir = config_dir or Path(os.environ.get("SOLALPHA_CONFIG_DIR", "./config"))
    profile_name = profile or os.environ.get("SOLALPHA_PROFILE", "default")

    base = _read_yaml(cfg_dir / "default.yaml")
    profile_data = _read_yaml(cfg_dir / f"{profile_name}.yaml") if profile_name != "default" else {}
    merged = _deep_merge(base, profile_data)
    if overrides:
        merged = _deep_merge(merged, overrides)

    # Map env-only conveniences into the merged dict before pydantic-settings runs,
    # since some keys live outside the nested structure.
    _apply_env_overrides(merged)

    try:
        return AppConfig.model_validate(merged)
    except ConfigError:
        raise
    except Exception as e:  # pydantic ValidationError, type issues
        raise ConfigError(f"config validation failed: {e}") from e


__all__ = [
    "AppConfig",
    "ExecutionConfig",
    "JupiterConfig",
    "KillSwitchConfig",
    "LoggingConfig",
    "MetricsConfig",
    "ModeManagerConfig",
    "ModeStr",
    "PaperExecutorConfig",
    "PersistenceConfig",
    "RaydiumConfig",
    "ResearchConfig",
    "RiskConfig",
    "RpcConfig",
    "SignalsClusterConfig",
    "SignalsConfig",
    "SignalsFlowAnomalyConfig",
    "SignalsPrePumpConfig",
    "SignalsWeightsConfig",
    "SmartWalletsConfig",
    "load_config",
]
