# INSTANT-AI-TRADER

Solana DEX-only autonomous trading bot for low capital (~$100) targeting micro-cap alpha via smart-wallet copy and early-launch sniping.

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
solana-keygen new -o secrets/trader.json --no-bip39-passphrase
cp .env.example .env            # fill HELIUS_API_KEY, etc.
jupyter notebook bot.ipynb
```

Run cells top-to-bottom. `DRY_RUN=1` in `.env` simulates trades without sending. Switch to `0` only after the canary phase passes.

## Architecture

```
Helius WS ──► ingest ──► queue ──► signal_engine ──► executor (Jupiter+Jito)
                │                       ▲                    │
                ├─ wallet_tracker ──────┤                    ▼
                └─ token_scanner ─► rug_filter        risk_manager → logger
```

- `src/ingest.py` — Helius WS subscriptions
- `src/wallet_tracker.py` — smart-wallet swap detection
- `src/token_scanner.py` — Raydium/Pump.fun new pool detection
- `src/rug_filter.py` — pre-trade safety checks
- `src/signal_engine.py` — dedupe + score
- `src/executor.py` — Jupiter v6 swap + Jito bundle
- `src/risk_manager.py` — position sizing, halts
- `src/position_manager.py` — TP ladder, SL, mirror-exit
- `src/logger.py` — SQLite + Telegram

## Verification

```bash
python -m tests.test_rug_filter        # unit tests
DRY_RUN=1 python bot.py                # full pipeline, no sends
python -m tests.latency_probe          # measure event→submit p50
```

## Risks

See `plans/` for full risk/mitigation table. **Do not run live without canary** ($20 cap, 1 open position, 24h review).

## Deployment

```bash
jupyter nbconvert --to script bot.ipynb
nohup python bot.py > bot.log 2>&1 &
```

Recommended VPS: Hetzner CX22 Frankfurt or Vultr NJ for Helius edge proximity.
