"""CLI: python -m src.research.collect --start ... --end ... --kind {wallets,prices,pools}."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

from .. import config as cfg_mod
from . import storage
from .collectors import helius_collector, pool_collector, price_collector


def _parse_dt(s: str) -> float:
    if s.isdigit():
        return float(s)
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()


async def _async_main(args):
    cfg = cfg_mod.load_config()
    api_key = cfg.helius_api_key
    if not api_key:
        print("HELIUS_API_KEY missing"); return 1

    start_ts = _parse_dt(args.start)
    end_ts = _parse_dt(args.end)

    if args.kind == "wallets":
        wallets = json.loads(Path(args.list or "data/smart_wallets.json").read_text())
        if not wallets:
            print("no wallets in list"); return 1
        out = await helius_collector.collect_addresses(api_key, wallets, start_ts, end_ts,
                                                      tracked_wallets=set(wallets))
        print(json.dumps(out, indent=2))
    elif args.kind == "pools":
        from ..constants import PUMPFUN, RAYDIUM_AMM_V4
        for p in (RAYDIUM_AMM_V4, PUMPFUN):
            r = await pool_collector.collect_program(api_key, p, start_ts, end_ts)
            print(p, r)
    elif args.kind == "prices":
        if not args.list:
            print("--list of mints required for prices"); return 1
        mints = json.loads(Path(args.list).read_text())
        out = await price_collector.collect_mints(os.getenv("BIRDEYE_API_KEY"),
                                                  mints, start_ts, end_ts)
        print(json.dumps(out, indent=2))
    else:
        print(f"unknown kind: {args.kind}"); return 1
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True, help="ISO date or unix ts")
    p.add_argument("--end", required=True, help="ISO date or unix ts")
    p.add_argument("--kind", choices=["wallets", "pools", "prices"], required=True)
    p.add_argument("--list", help="path to JSON list (wallets or mints)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    sys.exit(asyncio.run(_async_main(args)) or 0)


if __name__ == "__main__":
    main()
