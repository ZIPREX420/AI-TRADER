"""Jupiter v6 aggregator HTTP client.

Two endpoints we use:

  * `GET  /quote`             -- returns a `Quote` plus the opaque routing
                                 plan Jupiter expects back on the swap call.
  * `POST /swap-instructions` -- given a quote and a signer pubkey, returns
                                 serialized base64 instructions ready for the
                                 tx builder to assemble + sign.

Errors map onto `foundation.errors`: 4xx (except 429) -> `JupiterPermanentError`,
429/5xx/timeouts -> `JupiterError` (transient), unparseable -> permanent.
The retry-with-bump executor decides whether to retry based on these.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from solalpha.execution.base import Quote, SwapInstructions
from solalpha.foundation import metrics
from solalpha.foundation.errors import JupiterError, JupiterPermanentError
from solalpha.foundation.logging import get_logger

if TYPE_CHECKING:
    from solalpha.foundation.clock import Clock


_log = get_logger(__name__)


class JupiterClient:
    """Stateless wrapper around the Jupiter v6 REST API."""

    def __init__(
        self,
        base_url: str,
        clock: Clock,
        *,
        quote_timeout_s: float = 5.0,
        swap_timeout_s: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._quote_timeout = quote_timeout_s
        self._swap_timeout = swap_timeout_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(max(quote_timeout_s, swap_timeout_s)),
            http2=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- public ----

    async def quote(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
        only_direct_routes: bool = False,
    ) -> Quote:
        started = self._clock.monotonic()
        params: dict[str, str] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "true" if only_direct_routes else "false",
        }
        try:
            resp = await self._client.get(
                f"{self._base_url}/quote",
                params=params,
                timeout=self._quote_timeout,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            elapsed = self._clock.monotonic() - started
            metrics.QUOTE_LATENCY.labels(venue="jupiter", outcome="error").observe(elapsed)
            raise JupiterError(f"quote transport error: {e}") from e
        elapsed = self._clock.monotonic() - started
        outcome = "ok" if 200 <= resp.status_code < 300 else "error"
        metrics.QUOTE_LATENCY.labels(venue="jupiter", outcome=outcome).observe(elapsed)
        self._raise_for_status(resp, "quote")
        payload = self._parse_json(resp, "quote")
        return self._payload_to_quote(payload, input_mint, output_mint, slippage_bps)

    async def swap_instructions(
        self,
        *,
        quote: Quote,
        user_public_key: str,
        priority_fee_lamports: int,
    ) -> SwapInstructions:
        body = {
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
            "useSharedAccounts": True,
            "prioritizationFeeLamports": priority_fee_lamports,
            "asLegacyTransaction": False,
            "quoteResponse": quote.raw_response,
        }
        try:
            resp = await self._client.post(
                f"{self._base_url}/swap-instructions",
                json=body,
                timeout=self._swap_timeout,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise JupiterError(f"swap-instructions transport error: {e}") from e
        self._raise_for_status(resp, "swap-instructions")
        payload = self._parse_json(resp, "swap-instructions")
        return self._payload_to_swap_instructions(payload)

    # ---- helpers ----

    @staticmethod
    def _raise_for_status(resp: httpx.Response, what: str) -> None:
        if 200 <= resp.status_code < 300:
            return
        body_preview = resp.text[:200] if resp.text else ""
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise JupiterError(f"{what} {resp.status_code}: {body_preview}")
        raise JupiterPermanentError(f"{what} {resp.status_code}: {body_preview}")

    @staticmethod
    def _parse_json(resp: httpx.Response, what: str) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as e:
            raise JupiterPermanentError(f"{what} returned non-JSON: {e}") from e
        if not isinstance(data, dict):
            raise JupiterPermanentError(f"{what} returned non-object: {type(data).__name__}")
        return data

    @staticmethod
    def _payload_to_quote(
        payload: dict[str, Any],
        input_mint: str,
        output_mint: str,
        slippage_bps: int,
    ) -> Quote:
        try:
            in_amt = int(payload["inAmount"])
            out_amt = int(payload["outAmount"])
            other_min = int(payload.get("otherAmountThreshold", out_amt))
        except (KeyError, ValueError, TypeError) as e:
            raise JupiterPermanentError(f"quote shape invalid: {e}") from e
        impact_raw = payload.get("priceImpactPct", "0")
        try:
            impact = float(impact_raw)
        except (ValueError, TypeError):
            impact = 0.0
        plan_raw = payload.get("routePlan", []) or []
        route_plan: tuple[str, ...]
        if isinstance(plan_raw, list):
            route_plan = tuple(
                str(hop.get("swapInfo", {}).get("label", ""))
                for hop in plan_raw
                if isinstance(hop, dict)
            )
        else:
            route_plan = ()
        return Quote(
            venue="jupiter",
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount_raw=in_amt,
            out_amount_raw=out_amt,
            other_amount_threshold=other_min,
            price_impact_pct=impact,
            slippage_bps=slippage_bps,
            raw_response=payload,
            route_plan=route_plan,
        )

    @staticmethod
    def _payload_to_swap_instructions(payload: dict[str, Any]) -> SwapInstructions:
        def _b64_list(key: str) -> tuple[str, ...]:
            raw = payload.get(key, []) or []
            if not isinstance(raw, list):
                return ()
            return tuple(str(x) for x in raw if isinstance(x, str))

        swap_ix = payload.get("swapInstruction", "")
        if isinstance(swap_ix, dict):
            # Some Jupiter responses wrap the instruction in a dict; we
            # serialize it back to JSON so the tx builder can decode either
            # shape.
            import json as _json

            swap_ix_str = _json.dumps(swap_ix, sort_keys=True)
        else:
            swap_ix_str = str(swap_ix)
        alts_raw = payload.get("addressLookupTableAddresses", []) or []
        alts = (
            tuple(str(x) for x in alts_raw if isinstance(x, str))
            if isinstance(alts_raw, list)
            else ()
        )
        cu_limit = payload.get("computeUnitLimit")
        cu = int(cu_limit) if isinstance(cu_limit, (int, float)) else None
        return SwapInstructions(
            venue="jupiter",
            setup_instructions=_b64_list("setupInstructions"),
            swap_instruction=swap_ix_str,
            cleanup_instructions=_b64_list("cleanupInstructions"),
            address_lookup_tables=alts,
            compute_unit_limit=cu,
        )


__all__ = ["JupiterClient"]
