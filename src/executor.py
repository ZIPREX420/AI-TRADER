"""Jupiter v6 swap + Jito bundle submission with priority fee + tip."""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
from dataclasses import dataclass
from typing import Optional

import base58
import httpx
from solana.rpc.commitment import Confirmed
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from .constants import (
    JITO_TIP_ACCOUNTS,
    JUPITER_QUOTE_URL,
    JUPITER_SWAP_URL,
    LAMPORTS_PER_SOL,
    SOL_MINT,
)

log = logging.getLogger("executor")


@dataclass
class SwapResult:
    ok: bool
    signature: Optional[str]
    in_amount: int
    out_amount: int
    price_impact_pct: float
    route_summary: str
    error: Optional[str]
    elapsed_ms: float
    dry_run: bool


async def jupiter_quote(
    http: httpx.AsyncClient,
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int,
) -> Optional[dict]:
    try:
        r = await http.get(
            JUPITER_QUOTE_URL,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": slippage_bps,
                "swapMode": "ExactIn",
                "onlyDirectRoutes": "false",
                "asLegacyTransaction": "false",
                "maxAccounts": 64,
            },
            timeout=5.0,
        )
        if r.status_code != 200:
            log.warning(f"jupiter quote {r.status_code}: {r.text[:300]}")
            return None
        return r.json()
    except Exception as e:
        log.warning(f"jupiter quote err: {e!r}")
        return None


async def jupiter_swap_tx(
    http: httpx.AsyncClient,
    quote: dict,
    user_pubkey: str,
    priority_fee_lamports: int | str = "auto",
) -> Optional[str]:
    try:
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "useSharedAccounts": True,
            "asLegacyTransaction": False,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": priority_fee_lamports
            if isinstance(priority_fee_lamports, int)
            else "auto",
        }
        r = await http.post(JUPITER_SWAP_URL, json=body, timeout=8.0)
        if r.status_code != 200:
            log.warning(f"jupiter swap {r.status_code}: {r.text[:300]}")
            return None
        data = r.json()
        return data.get("swapTransaction")
    except Exception as e:
        log.warning(f"jupiter swap err: {e!r}")
        return None


def _sign_versioned(tx_b64: str, kp: Keypair) -> VersionedTransaction:
    raw = base64.b64decode(tx_b64)
    unsigned = VersionedTransaction.from_bytes(raw)
    return VersionedTransaction(unsigned.message, [kp])


async def _build_jito_tip_tx(
    kp: Keypair,
    tip_lamports: int,
    recent_blockhash: Hash,
) -> VersionedTransaction:
    tip_account = Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS))
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=tip_account, lamports=tip_lamports))
    msg = MessageV0.try_compile(
        payer=kp.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash,
    )
    return VersionedTransaction(msg, [kp])


async def submit_jito_bundle(
    http: httpx.AsyncClient,
    jito_url: str,
    txs: list[VersionedTransaction],
) -> Optional[str]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendBundle",
        "params": [[base58.b58encode(bytes(t)).decode() for t in txs]],
    }
    try:
        r = await http.post(f"{jito_url}/api/v1/bundles", json=payload, timeout=8.0)
        if r.status_code != 200:
            log.warning(f"jito bundle {r.status_code}: {r.text[:300]}")
            return None
        data = r.json()
        return data.get("result")
    except Exception as e:
        log.warning(f"jito bundle err: {e!r}")
        return None


async def submit_rpc(client, signed: VersionedTransaction) -> Optional[str]:
    """Fallback: send directly via RPC."""
    try:
        from solana.rpc.types import TxOpts
        resp = await client.send_raw_transaction(
            bytes(signed),
            opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3),
        )
        return str(resp.value)
    except Exception as e:
        log.warning(f"rpc submit err: {e!r}")
        return None


async def confirm_signature(client, sig_str: str, timeout_s: float = 30.0) -> bool:
    """Poll signature status until confirmed/finalized or timeout."""
    sig = Signature.from_string(sig_str)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            r = await client.get_signature_statuses([sig])
            v = r.value[0] if r.value else None
            if v and v.confirmation_status and v.err is None:
                return True
            if v and v.err:
                return False
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return False


async def execute_swap(
    *,
    client,
    http: httpx.AsyncClient,
    kp: Keypair,
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int,
    dry_run: bool,
    jito_url: str,
    jito_tip_lamports: int,
    use_jito: bool = True,
) -> SwapResult:
    t0 = time.time()
    quote = await jupiter_quote(http, input_mint, output_mint, amount_lamports, slippage_bps)
    if quote is None:
        return SwapResult(False, None, amount_lamports, 0, 0.0, "", "no_quote",
                          (time.time() - t0) * 1000, dry_run)
    out_amount = int(quote.get("outAmount", 0))
    pi = float(quote.get("priceImpactPct", 0) or 0)
    routes = ",".join(rp["swapInfo"]["label"] for rp in quote.get("routePlan", []) if "swapInfo" in rp)

    if dry_run:
        return SwapResult(True, None, amount_lamports, out_amount, pi, routes, "dry_run",
                          (time.time() - t0) * 1000, True)

    swap_b64 = await jupiter_swap_tx(http, quote, str(kp.pubkey()),
                                     priority_fee_lamports="auto")
    if swap_b64 is None:
        return SwapResult(False, None, amount_lamports, out_amount, pi, routes, "no_swap_tx",
                          (time.time() - t0) * 1000, dry_run)

    signed = _sign_versioned(swap_b64, kp)

    sig_str: Optional[str] = None
    if use_jito and jito_url:
        try:
            blockhash_resp = await client.get_latest_blockhash()
            bh = blockhash_resp.value.blockhash
            tip_tx = await _build_jito_tip_tx(kp, jito_tip_lamports, bh)
            await submit_jito_bundle(http, jito_url, [signed, tip_tx])
            # Jito bundle returns bundle id, not tx sig; we use the swap tx sig
            sig_str = str(signed.signatures[0])
        except Exception as e:
            log.warning(f"jito path failed: {e!r}; falling back to RPC")
            sig_str = await submit_rpc(client, signed)
    else:
        sig_str = await submit_rpc(client, signed)

    if sig_str is None:
        return SwapResult(False, None, amount_lamports, out_amount, pi, routes, "submit_failed",
                          (time.time() - t0) * 1000, dry_run)

    confirmed = await confirm_signature(client, sig_str, timeout_s=30.0)
    if not confirmed:
        return SwapResult(False, sig_str, amount_lamports, out_amount, pi, routes, "not_confirmed",
                          (time.time() - t0) * 1000, dry_run)

    return SwapResult(True, sig_str, amount_lamports, out_amount, pi, routes, None,
                      (time.time() - t0) * 1000, dry_run)
