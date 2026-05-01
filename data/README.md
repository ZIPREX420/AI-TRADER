# Smart wallet curation

`smart_wallets.json` is a JSON array of base58 Solana pubkeys (strings).

## How to source — do this manually, do not commit pubkeys without vetting

1. **Cielo Finance** — https://app.cielo.finance/leaderboards
   - Filter: chain=Solana, period=30d, win-rate ≥ 65%, realized PnL ≥ +50 SOL, trade count ≥ 20.
   - Avoid wallets with one outlier trade dominating PnL (check the trade list).

2. **Birdeye** — https://birdeye.so/leaderboard?chain=solana
   - Filter top traders by 7d/30d realized PnL.

3. **GMGN** — https://gmgn.ai/?chain=sol
   - "Smart Money" tab; sort by realized PnL.

4. **Dune dashboards** — search "solana smart wallets memecoin".

## Validation checklist (per wallet)

- [ ] Realized PnL > unrealized PnL (avoid bag-holders).
- [ ] Win rate ≥ 60% on swaps, not just transfers.
- [ ] No obvious wash trading (back-and-forth between two wallets).
- [ ] Active in last 7 days.
- [ ] Not a known MM/aggregator/exchange hot wallet.

## Format

```json
[
  "WALLET_PUBKEY_1",
  "WALLET_PUBKEY_2"
]
```

Refresh weekly. Drop wallets whose 7-day win rate falls below 40%.
