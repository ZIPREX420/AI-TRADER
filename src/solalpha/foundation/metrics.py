"""Prometheus metrics registry and shared instruments.

All metrics live under the `solalpha_` namespace. Instruments are created once
at import-time and reused across the codebase; tests can `reset()` the global
registry for isolation.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=True)


# ---- Data plane ----
EVENTS_INGESTED = Counter(
    "solalpha_events_ingested_total",
    "Raw events received from RPCs.",
    labelnames=["source"],
    registry=REGISTRY,
)
EVENTS_DEDUPED = Counter(
    "solalpha_events_deduped_total",
    "Duplicate events dropped.",
    registry=REGISTRY,
)
DECODER_UNKNOWN = Counter(
    "solalpha_decoder_unknown_program_total",
    "Transactions referencing unknown program ids.",
    labelnames=["program_id"],
    registry=REGISTRY,
)
DECODER_ERRORS = Counter(
    "solalpha_decoder_errors_total",
    "Decoder failures by program.",
    labelnames=["program"],
    registry=REGISTRY,
)
SWAPS_NORMALIZED = Counter(
    "solalpha_swaps_normalized_total",
    "Successfully normalized swaps.",
    labelnames=["program"],
    registry=REGISTRY,
)

# ---- RPC pool ----
RPC_REQUEST_LATENCY = Histogram(
    "solalpha_rpc_request_latency_seconds",
    "RPC request latency.",
    labelnames=["endpoint", "method", "outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
    registry=REGISTRY,
)
RPC_QUARANTINED = Gauge(
    "solalpha_rpc_quarantined",
    "1 if endpoint is currently quarantined.",
    labelnames=["endpoint"],
    registry=REGISTRY,
)
RPC_HEALTHY = Gauge(
    "solalpha_rpc_healthy_endpoints",
    "Count of currently healthy RPC endpoints.",
    registry=REGISTRY,
)

# ---- Signal / risk ----
SIGNALS_EMITTED = Counter(
    "solalpha_signals_emitted_total",
    "Signals produced by detectors.",
    labelnames=["detector"],
    registry=REGISTRY,
)
RISK_DECISIONS = Counter(
    "solalpha_risk_decisions_total",
    "Risk-engine decisions.",
    labelnames=["decision"],
    registry=REGISTRY,
)
RISK_REJECTIONS = Counter(
    "solalpha_risk_rejections_total",
    "Risk-engine rejection reasons.",
    labelnames=["reason"],
    registry=REGISTRY,
)
KILL_SWITCH_ARMED = Gauge(
    "solalpha_kill_switch_armed",
    "1 if kill switch is armed.",
    registry=REGISTRY,
)
DAILY_PNL_USD = Gauge(
    "solalpha_daily_pnl_usd",
    "Realized PnL for the current UTC day.",
    registry=REGISTRY,
)
OPEN_POSITIONS = Gauge(
    "solalpha_open_positions",
    "Currently open positions.",
    registry=REGISTRY,
)
MODE_GAUGE = Gauge(
    "solalpha_mode",
    "Current ModeManager state (encoded numeric).",
    labelnames=["mode"],
    registry=REGISTRY,
)

# ---- Execution ----
QUOTE_LATENCY = Histogram(
    "solalpha_quote_latency_seconds",
    "Time to receive quote.",
    labelnames=["venue", "outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    registry=REGISTRY,
)
SUBMIT_LATENCY = Histogram(
    "solalpha_submit_latency_seconds",
    "Time from build to submit.",
    labelnames=["outcome"],
    registry=REGISTRY,
)
CONFIRM_LATENCY = Histogram(
    "solalpha_confirm_latency_seconds",
    "Time from submit to confirm.",
    labelnames=["outcome"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60),
    registry=REGISTRY,
)
ORDERS_TOTAL = Counter(
    "solalpha_orders_total",
    "Orders placed by terminal status.",
    labelnames=["status"],
    registry=REGISTRY,
)
RETRY_BUMPS = Counter(
    "solalpha_retry_bumps_total",
    "Priority-fee bumps applied.",
    labelnames=["attempt"],
    registry=REGISTRY,
)
STUCK_TX = Gauge(
    "solalpha_stuck_tx",
    "Transactions awaiting reconciliation.",
    registry=REGISTRY,
)

# ---- Recovery ----
RECOVERY_RUNS = Counter(
    "solalpha_recovery_runs_total",
    "Recovery invocations.",
    labelnames=["outcome"],
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    """Render the Prometheus exposition format."""
    return generate_latest(REGISTRY)


__all__ = [
    "CONFIRM_LATENCY",
    "DAILY_PNL_USD",
    "DECODER_ERRORS",
    "DECODER_UNKNOWN",
    "EVENTS_DEDUPED",
    "EVENTS_INGESTED",
    "KILL_SWITCH_ARMED",
    "MODE_GAUGE",
    "OPEN_POSITIONS",
    "ORDERS_TOTAL",
    "QUOTE_LATENCY",
    "RECOVERY_RUNS",
    "REGISTRY",
    "RETRY_BUMPS",
    "RISK_DECISIONS",
    "RISK_REJECTIONS",
    "RPC_HEALTHY",
    "RPC_QUARANTINED",
    "RPC_REQUEST_LATENCY",
    "SIGNALS_EMITTED",
    "STUCK_TX",
    "SUBMIT_LATENCY",
    "SWAPS_NORMALIZED",
    "render_metrics",
]
