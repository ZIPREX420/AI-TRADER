"""Settings dataclass + .env loader. Lazy keypair load."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(key)
    return v if v not in (None, "") else default


def _b(key: str, default: bool = False) -> bool:
    v = _env(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _f(key: str, default: float) -> float:
    v = _env(key)
    return float(v) if v is not None else default


def _i(key: str, default: int) -> int:
    v = _env(key)
    return int(v) if v is not None else default


@dataclass
class Settings:
    helius_api_key: str
    quicknode_http: str
    quicknode_ws: str
    jito_url: str
    jito_tip_lamports: int
    keypair_path: str
    capital_sol: float
    max_position_pct: float
    max_open: int
    sol_reserve: float
    fee_threshold_pct: float
    slippage_bps_min: int
    slippage_bps_max: int
    daily_loss_halt_pct: float
    mode: str
    db_path: str
    log_level: str
    dry_run: bool
    state_dir: str = "data/state"
    logs_dir: str = "data/logs"

    @property
    def helius_http(self) -> str:
        if not self.helius_api_key:
            return ""
        return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"

    @property
    def helius_ws(self) -> str:
        if not self.helius_api_key:
            return ""
        return f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"

    @property
    def halt_path(self) -> str:
        return os.path.join(self.state_dir, "HALT")


def load() -> Settings:
    return Settings(
        helius_api_key=_env("HELIUS_API_KEY", "") or "",
        quicknode_http=_env("QUICKNODE_RPC_URL", "") or "",
        quicknode_ws=_env("QUICKNODE_WS_URL", "") or "",
        jito_url=_env("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf") or "",
        jito_tip_lamports=_i("JITO_TIP_LAMPORTS", 12_500),
        keypair_path=_env("KEYPAIR_PATH", "secrets/trader.json") or "secrets/trader.json",
        capital_sol=_f("CAPITAL_SOL", 0.667),
        max_position_pct=_f("MAX_POSITION_PCT", 0.07),
        max_open=_i("MAX_OPEN", 3),
        sol_reserve=_f("SOL_RESERVE", 0.05),
        fee_threshold_pct=_f("FEE_THRESHOLD_PCT", 0.015),
        slippage_bps_min=_i("SLIPPAGE_BPS_MIN", 200),
        slippage_bps_max=_i("SLIPPAGE_BPS_MAX", 800),
        daily_loss_halt_pct=_f("DAILY_LOSS_HALT_PCT", -0.30),
        mode=_env("MODE", "paper") or "paper",
        db_path=_env("DB_PATH", "data/logs/mvp.db") or "data/logs/mvp.db",
        log_level=_env("LOG_LEVEL", "INFO") or "INFO",
        dry_run=_b("DRY_RUN", True),
    )


def load_keypair(path: str):
    """Lazy-import solders only when actually needed (live mode)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"keypair not found: {path}")
    raw = json.loads(p.read_text())
    from solders.keypair import Keypair
    return Keypair.from_bytes(bytes(raw))
