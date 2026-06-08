# ADR 0001 — Resolve the `solana-py` / `websockets` conflict by version bump, standardize on `solders`

- **Status:** Accepted
- **Date:** 2026-05-15
- **Deciders:** Seppe Willemsens

## Context

`pyproject.toml` originally pinned `solana>=0.34,<0.36`. Every `solana-py`
release in that range depends on `websockets>=9,<12`, but solalpha's data
plane needs `websockets>=12` for the `logsSubscribe` reconnect path it
relies on. The two constraints have no common solution, so `pip install -e
".[dev]"` failed to resolve on a clean environment — a hard blocker for
Phase 1.

Two paths were available:

1. **Drop `solana-py` entirely** and hand-roll a minimal typed JSON-RPC
   client. The data plane already needs a custom multi-endpoint pool
   (`data/rpc_pool.py`) with rolling-score failover and quarantine, which
   `solana-py`'s `AsyncClient` does not provide, so most of `solana-py`'s
   surface was going unused anyway.
2. **Bump the pin** to a `solana-py` release whose `websockets` constraint
   is compatible, and keep using it only where it genuinely helps.

## Decision

Bump the pins to `solana>=0.36,<0.37` and `solders>=0.26,<0.28`, and
**standardize on `solders` for all on-chain primitives** — `Pubkey`,
`Keypair`, `Hash`, `Instruction`, `MessageV0`, `VersionedTransaction`,
compute-budget instructions, and the Address Lookup Table account type.

`solana-py` 0.36 relaxed its `websockets` ceiling, which removes the
resolution conflict. We retain `solana` as a declared dependency because it
ships `solders` as a coherent, co-versioned pair and the pin documents the
tested combination; we do **not** import `solana.*` high-level clients in
`src/solalpha/`. All RPC traffic goes through the custom `RpcPool`, and all
transaction construction goes through `execution/tx_builder.py` using
`solders` types directly.

## Consequences

**Positive**

- Clean install on Python 3.11 and 3.12; CI can resolve dependencies.
- No bespoke JSON-RPC client to write, test, and maintain — `RpcPool`
  stays focused on pooling/failover, not wire-format plumbing.
- `solders` is a Rust-backed, strongly-typed library; transaction building
  is fast and type-checks cleanly under `mypy --strict` (with
  `ignore_missing_imports` for the `solders.*` stub gap).

**Negative**

- `solana-py` remains an installed dependency that the code does not
  directly import, so a future `websockets` major bump could resurface a
  resolution conflict. Mitigated by the `security.yml` `pip-audit` job and
  the upper bounds on both pins.
- The `solders.*` packages lack type stubs, so they sit under a
  `[[tool.mypy.overrides]] ignore_missing_imports = true` block — type
  safety at those call sites is asserted by tests, not the type checker.

## Follow-ups

- Revisit dropping `solana` from `dependencies` entirely once it is
  confirmed that no transitive consumer needs it; if removed, pin
  `solders` on its own.
- Track `solders` type-stub availability; remove the mypy override if
  upstream ships stubs.
