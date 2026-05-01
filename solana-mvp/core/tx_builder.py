"""Versioned-tx parsing/signing/serialization. Jito tip-tx builder.

Fully usable in production. Tests can monkey-patch the signing path or use the
`SimpleSigner` test stub which avoids importing solders.
"""
from __future__ import annotations

import base64
import logging
import random
from typing import Any, Optional

import base58

log = logging.getLogger("tx_builder")

JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pivKeVQqkPtXWAnyD8s",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]


class TxBuilderError(Exception):
    pass


def parse_swap_tx_b64(tx_b64: str):
    """Returns a solders VersionedTransaction. Raises TxBuilderError on failure."""
    try:
        from solders.transaction import VersionedTransaction
    except Exception as e:
        raise TxBuilderError(f"solders unavailable: {e}")
    try:
        raw = base64.b64decode(tx_b64)
        return VersionedTransaction.from_bytes(raw)
    except Exception as e:
        raise TxBuilderError(f"parse: {e}")


def sign_versioned(unsigned_tx, keypair):
    """Returns a signed VersionedTransaction. `keypair` may be a real solders Keypair
    or any object with a .pubkey() method (used by tests with SimpleSigner)."""
    try:
        from solders.transaction import VersionedTransaction
    except Exception as e:
        raise TxBuilderError(f"solders unavailable: {e}")
    try:
        return VersionedTransaction(unsigned_tx.message, [keypair])
    except Exception as e:
        raise TxBuilderError(f"sign: {e}")


def build_tip_tx(keypair, tip_lamports: int, recent_blockhash, tip_account: Optional[str] = None):
    """Builds a SOL transfer to a Jito tip account."""
    try:
        from solders.instruction import Instruction  # noqa: F401
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer
        from solders.transaction import VersionedTransaction
    except Exception as e:
        raise TxBuilderError(f"solders unavailable: {e}")
    tip_account = tip_account or random.choice(JITO_TIP_ACCOUNTS)
    ix = transfer(TransferParams(
        from_pubkey=keypair.pubkey(),
        to_pubkey=Pubkey.from_string(tip_account),
        lamports=int(tip_lamports),
    ))
    msg = MessageV0.try_compile(
        payer=keypair.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash,
    )
    return VersionedTransaction(msg, [keypair])


def serialize_b58(signed_tx) -> str:
    return base58.b58encode(bytes(signed_tx)).decode("ascii")


def signature_str(signed_tx) -> str:
    """Returns the first signature (the tx id) as base58 string."""
    sigs = getattr(signed_tx, "signatures", None) or []
    if not sigs:
        return ""
    return str(sigs[0])
