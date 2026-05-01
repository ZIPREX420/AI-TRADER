# solana-mvp (v9)

Production Solana DEX live trading system. Async-first. Mode-aware. Self-degrading to paper on RPC/exec failures.

## Install

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
solana-keygen new -o secrets/trader.json --no-bip39-passphrase
cp .env.example .env                                  # fill HELIUS_API_KEY etc.
```

## Test

```bash
pytest -q
```

## Run (paper mode by default)

```bash
python -m runtime.loop --mode paper
python -m runtime.loop --mode live      # requires real keypair + funded wallet
```

## Manual halt

`touch data/state/HALT` → system stops opening positions; existing positions still managed.

## Architecture

```
RPC (Helius+QuickNode WS pool)
     │
     ▼ NormalizedEvent (dedup sig+slot)
wallet_tracker_live ─► tx_decoder ─► WalletEvent
                                          │
                                          ▼
                            cluster_detector ─► PrePumpSignal
                                          │
                                          ▼
                                  signal_engine ─► Candidate
                                          │
                                  risk_exec.gates
                                          │
                                  mode_manager.executor()
                                          │
                                LiveExecutor / MockExecutor
                                          │
                              quote → build → sign → submit → confirm
                                          │
                                  feedback (parquet + EWMA + telemetry)
```

Modes: `LIVE → DEGRADED_RPC → DEGRADED_EXEC → PAPER`. `HALT` overrides all.
