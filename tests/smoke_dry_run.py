"""Live RPC smoke test (read-only). Verifies env + RPC + Jupiter quote works end-to-end.

Usage:
    DRY_RUN=1 python tests/smoke_dry_run.py
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from solana.rpc.async_api import AsyncClient

from src.config import load_config
from src.constants import SOL_MINT, USDC_MINT, LAMPORTS_PER_SOL
from src.executor import jupiter_quote
from src.rug_filter import run_checks


async def main():
    cfg = load_config()
    if not cfg.helius_api_key:
        print("FAIL: HELIUS_API_KEY missing in .env"); return 1
    client = AsyncClient(cfg.helius_http)
    http = httpx.AsyncClient()
    try:
        v = await client.get_version()
        print(f"rpc_version={v.value.solana_core}")

        slot = await client.get_slot()
        print(f"current_slot={slot.value}")

        q = await jupiter_quote(http, SOL_MINT, USDC_MINT, int(0.1 * LAMPORTS_PER_SOL), slippage_bps=50)
        if q is None:
            print("FAIL: jupiter quote returned None"); return 1
        print(f"jupiter_quote_outAmount={q.get('outAmount')} priceImpact={q.get('priceImpactPct')}")

        # rug filter against USDC (should pass everything except authorities — USDC has them)
        # Use BONK as a known clean meme
        BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        res = await run_checks(client, http, BONK)
        print(f"BONK rug_filter: ok={res.ok} reason={res.reason} top10={res.top10_pct:.2%}")

        print("SMOKE_OK")
        return 0
    finally:
        await http.aclose()
        await client.close()


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.exit(code or 0)
