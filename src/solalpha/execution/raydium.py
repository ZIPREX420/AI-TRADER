"""Raydium v3 REST client -- fallback router when Jupiter is unhealthy.

Raydium's v3 API surface used here:

  * `GET /compute/swap-base-in`     -- price quote (analogous to Jupiter `/quote`).
  * `GET /transaction/swap-base-in` -- serialized transaction payload.

This is intentionally a thin fallback: the live executor prefers Jupiter
unless `DEGRADED_EXEC` (Jupiter probe down) is set, at which point the
route selector promotes Raydium. The API shapes differ from Jupiter
enough that we expose only `quote` and `swap_transaction` and leave the
"swap instructions" decomposition to the tx builder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from solalpha.execution.base import Quote, SwapInstructions
from solalpha.foundation import metrics
from solalpha.foundation.errors import RaydiumError
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.foundation.clock import Clock


_log = get_logger(__name__)


class RaydiumClient:
    """Stateless wrapper around the Raydium v3 REST API."""

    def __init__(
        self,
        base_url: str,
        clock: Clock,
        *,
        request_timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._timeout = request_timeout_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(request_timeout_s),
            http2=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def quote(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
    ) -> Quote:
        started = self._clock.monotonic()
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps),
            "txVersion": "V0",
        }
        try:
            resp = await self._client.get(
                f"{self._base_url}/compute/swap-base-in",
                params=params,
                timeout=self._timeout,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            metrics.QUOTE_LATENCY.labels(venue="raydium", outcome="error").observe(
                self._clock.monotonic() - started
            )
            raise RaydiumError(f"quote transport error: {e}") from e
        elapsed = self._clock.monotonic() - started
        outcome = "ok" if 200 <= resp.status_code < 300 else "error"
        metrics.QUOTE_LATENCY.labels(venue="raydium", outcome=outcome).observe(elapsed)
        if resp.status_code >= 400:
            raise RaydiumError(f"quote {resp.status_code}: {resp.text[:200]}")
        payload = self._json(resp, "quote")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RaydiumError(f"quote shape: {payload!r}")
        try:
            in_amt = int(data["inputAmount"])
            out_amt = int(data["outputAmount"])
            other_min = int(data.get("otherAmountThreshold", out_amt))
        except (KeyError, ValueError, TypeError) as e:
            raise RaydiumError(f"quote fields: {e}") from e
        try:
            impact = float(data.get("priceImpactPct", 0.0))
        except (TypeError, ValueError):
            impact = 0.0
        return Quote(
            venue="raydium",
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount_raw=in_amt,
            out_amount_raw=out_amt,
            other_amount_threshold=other_min,
            price_impact_pct=impact,
            slippage_bps=slippage_bps,
            raw_response=payload,
        )

    async def swap_transaction(
        self,
        *,
        quote: Quote,
        user_public_key: str,
        priority_fee_lamports: int,
    ) -> SwapInstructions:
        body = {
            "wallet": user_public_key,
            "computeUnitPriceMicroLamports": str(priority_fee_lamports),
            "swapResponse": quote.raw_response,
            "txVersion": "V0",
            "wrapSol": True,
            "unwrapSol": True,
        }
        try:
            resp = await self._client.post(
                f"{self._base_url}/transaction/swap-base-in",
                json=body,
                timeout=self._timeout,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise RaydiumError(f"swap-transaction transport error: {e}") from e
        if resp.status_code >= 400:
            raise RaydiumError(f"swap-transaction {resp.status_code}: {resp.text[:200]}")
        payload = self._json(resp, "swap-transaction")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            raise RaydiumError("swap-transaction empty data")
        first = data[0]
        if not isinstance(first, dict):
            raise RaydiumError(f"swap-transaction bad shape: {first!r}")
        tx_b64 = str(first.get("transaction", ""))
        # Raydium returns a full signed-by-no-one V0 transaction. We wrap it
        # under `swap_instruction` so the tx builder treats it as a single
        # opaque payload; ALT addresses, when present, ride alongside.
        alts_raw = first.get("addressLookupTableAddresses", []) or []
        alts = (
            tuple(str(x) for x in alts_raw if isinstance(x, str))
            if isinstance(alts_raw, list)
            else ()
        )
        return SwapInstructions(
            venue="raydium",
            swap_instruction=tx_b64,
            address_lookup_tables=alts,
        )

    @staticmethod
    def _json(resp: httpx.Response, what: str) -> dict[str, Any]:
        try:
            obj = resp.json()
        except ValueError as e:
            raise RaydiumError(f"{what} non-JSON: {e}") from e
        if not isinstance(obj, dict):
            raise RaydiumError(f"{what} non-object: {type(obj).__name__}")
        return obj


__all__ = ["RaydiumClient"]
