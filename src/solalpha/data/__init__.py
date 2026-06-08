"""Data plane: multi-RPC ingestion, decoding, normalization, caches.

Pipeline:

    WebSocketIngestor / BackfillPoller
        \
         +--> DedupeRing -> EVENTS_TOPIC -> DecoderWorker -> NORMALIZED_TOPIC
        /                                       |
    RpcPool ----------------------------------- + (getTransaction fetch)

`SmartWalletSubscriptionManager` maintains the wallet list the ingestor
subscribes to. Mint/Pool/Ata caches are fetch-through (mint) or
write-through (pool, ata-owner) backed by the `cache_*` SQLite tables.
"""

from __future__ import annotations

from solalpha.data.backfill_poller import BackfillPoller
from solalpha.data.caches import (
    AtaOwner,
    AtaOwnerCache,
    MintMetadata,
    MintMetadataCache,
    PoolCache,
    PoolMeta,
)
from solalpha.data.decoder import (
    QUOTE_MINTS,
    USDC_MINT,
    USDT_MINT,
    WSOL_MINT,
    TransactionDecoder,
)
from solalpha.data.decoder_worker import DecoderWorker
from solalpha.data.dedupe import DedupeRing
from solalpha.data.program_ids import (
    DECODABLE_PROGRAMS,
    PROGRAM_ADDRESS_LOOKUP_TABLE,
    PROGRAM_ASSOCIATED_TOKEN,
    PROGRAM_COMPUTE_BUDGET,
    PROGRAM_JUPITER_V6,
    PROGRAM_ORCA_WHIRLPOOL,
    PROGRAM_PUMPFUN,
    PROGRAM_RAYDIUM_AMM_V4,
    PROGRAM_SPL_TOKEN,
    PROGRAM_SPL_TOKEN_2022,
    PROGRAM_SYSTEM,
)
from solalpha.data.rpc_pool import RpcPool
from solalpha.data.smart_wallet_subscriptions import SmartWalletSubscriptionManager
from solalpha.data.websocket_ingestor import WebSocketIngestor

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
    "QUOTE_MINTS",
    "USDC_MINT",
    "USDT_MINT",
    "WSOL_MINT",
    "AtaOwner",
    "AtaOwnerCache",
    "BackfillPoller",
    "DecoderWorker",
    "DedupeRing",
    "MintMetadata",
    "MintMetadataCache",
    "PoolCache",
    "PoolMeta",
    "RpcPool",
    "SmartWalletSubscriptionManager",
    "TransactionDecoder",
    "WebSocketIngestor",
]
