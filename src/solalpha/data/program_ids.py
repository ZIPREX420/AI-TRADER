"""Canonical Solana program-id constants.

These are the only program ids solalpha decodes or builds against. The
decoder dispatch table is keyed on `PROGRAM_*` values here. Add a new entry
only after a sub-decoder lands for it.

Sources (mainnet-beta):
  * Jupiter Aggregator v6 -- the dominant routing layer on mainnet.
  * Raydium AMM v4         -- direct AMM swaps + fallback routing.
  * Orca Whirlpools        -- concentrated-liquidity DEX.
  * pump.fun bonding curve -- memecoin launch venue.
  * SPL Token / Associated Token -- token mint + ATA derivation.
  * Compute Budget         -- priority-fee + compute-unit-limit instructions.
  * Address Lookup Table   -- Versioned-tx ALT program.
"""

from __future__ import annotations

# ---- DEX / routing ----
PROGRAM_JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
PROGRAM_RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PROGRAM_ORCA_WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
PROGRAM_PUMPFUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ---- Token / ATA ----
# These are public on-chain program ids, not secrets; the bandit heuristic
# flags them only because the names contain "Token".
PROGRAM_SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105
PROGRAM_SPL_TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"  # noqa: S105
PROGRAM_ASSOCIATED_TOKEN = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"  # noqa: S105

# ---- System / utility ----
PROGRAM_SYSTEM = "11111111111111111111111111111111"
PROGRAM_COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
PROGRAM_ADDRESS_LOOKUP_TABLE = "AddressLookupTab1e1111111111111111111111111"

# Convenience: every program we know how to decode. Used as the
# allow-list filter for transaction-level dispatch in `decoder.py`.
DECODABLE_PROGRAMS: frozenset[str] = frozenset(
    {
        PROGRAM_JUPITER_V6,
        PROGRAM_RAYDIUM_AMM_V4,
        PROGRAM_ORCA_WHIRLPOOL,
        PROGRAM_PUMPFUN,
    }
)

__all__ = [
    "DECODABLE_PROGRAMS",
    "PROGRAM_ADDRESS_LOOKUP_TABLE",
    "PROGRAM_ASSOCIATED_TOKEN",
    "PROGRAM_COMPUTE_BUDGET",
    "PROGRAM_JUPITER_V6",
    "PROGRAM_ORCA_WHIRLPOOL",
    "PROGRAM_PUMPFUN",
    "PROGRAM_RAYDIUM_AMM_V4",
    "PROGRAM_SPL_TOKEN",
    "PROGRAM_SPL_TOKEN_2022",
    "PROGRAM_SYSTEM",
]
