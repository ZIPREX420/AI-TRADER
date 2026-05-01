import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from solders.keypair import Keypair

load_dotenv()


@dataclass
class Config:
    helius_api_key: str
    quicknode_rpc_url: str
    jito_url: str
    jito_tip_lamports: int
    keypair_path: str
    telegram_bot_token: str
    telegram_chat_id: str
    dry_run: bool
    capital_usd: float
    max_position_pct: float
    max_open_positions: int
    daily_loss_halt_pct: float
    sol_reserve: float
    slippage_bps: int
    db_path: str
    log_level: str

    @property
    def helius_http(self) -> str:
        return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"

    @property
    def helius_ws(self) -> str:
        return f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"

    @property
    def helius_atlas_ws(self) -> str:
        return f"wss://atlas-mainnet.helius-rpc.com/?api-key={self.helius_api_key}"


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_config() -> Config:
    return Config(
        helius_api_key=os.getenv("HELIUS_API_KEY", ""),
        quicknode_rpc_url=os.getenv("QUICKNODE_RPC_URL", ""),
        jito_url=os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf"),
        jito_tip_lamports=int(os.getenv("JITO_TIP_LAMPORTS", "10000")),
        keypair_path=os.getenv("KEYPAIR_PATH", "secrets/trader.json"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        dry_run=_bool(os.getenv("DRY_RUN"), True),
        capital_usd=float(os.getenv("CAPITAL_USD", "100")),
        max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.07")),
        max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "3")),
        daily_loss_halt_pct=float(os.getenv("DAILY_LOSS_HALT_PCT", "-0.30")),
        sol_reserve=float(os.getenv("SOL_RESERVE", "0.05")),
        slippage_bps=int(os.getenv("SLIPPAGE_BPS", "500")),
        db_path=os.getenv("DB_PATH", "data/trades.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


def load_keypair(path: str) -> Keypair:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Keypair not found at {path}. Run: solana-keygen new -o {path}")
    raw = json.loads(p.read_text())
    return Keypair.from_bytes(bytes(raw))


def load_smart_wallets(path: str = "data/smart_wallets.json") -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text())
