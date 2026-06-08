"""Top-level Application orchestrator.

`Application(cfg).run()` is what the CLI's `paper` / `live` / `run` commands
ultimately call. It:
  * builds the runtime dependencies (Clock, Bus, stores, HealthRegistry)
  * runs `observability.recovery.recover()` so we resume in a consistent state
  * constructs the Phase 1 spine -- KillSwitch, PortfolioTracker,
    SnapshotManager, MetricsServer (if enabled), ModeManager
  * if `rpc.urls` is non-empty, wires the Phase 2 data plane -- RpcPool,
    DedupeRing, DecoderWorker, BackfillPoller, WebSocketIngestor,
    SmartWalletSubscriptionManager
  * supervises all workers in a single `anyio.TaskGroup`, restarting crashed
    ones with backoff and respecting mode gating
  * installs SIGINT/SIGTERM handlers for graceful shutdown (and, on POSIX,
    a SIGHUP handler that hot-reloads the RPC endpoint set), after which it
    takes one final snapshot and closes the stores

Signal-pipeline / execution / research planes plug in here as they land in
Phases 3-5 by registering additional `Worker`s on the supervisor.
"""

from __future__ import annotations

import contextlib
import json
import signal
from typing import TYPE_CHECKING

import anyio

from solalpha.data import (
    DECODABLE_PROGRAMS,
    BackfillPoller,
    DecoderWorker,
    DedupeRing,
    MintMetadataCache,
    RpcPool,
    SmartWalletSubscriptionManager,
    WebSocketIngestor,
)
from solalpha.execution import (
    AltManager,
    ExecutionPipeline,
    JupiterClient,
    LiveExecutor,
    PaperExecutor,
    RaydiumClient,
    RouteSelector,
    StuckTxResolver,
    TxBuilder,
)
from solalpha.foundation.bus import Bus
from solalpha.foundation.clock import SystemClock
from solalpha.foundation.health import HealthRegistry
from solalpha.foundation.logging import get_logger
from solalpha.foundation.secrets import KeypairLoader
from solalpha.foundation.state import ParquetStore, SqliteStore
from solalpha.observability.exporter import MetricsServer
from solalpha.observability.portfolio import PortfolioTracker
from solalpha.observability.recovery import recover
from solalpha.observability.snapshot import SnapshotManager
from solalpha.observability.trade_log import TradeLog
from solalpha.runtime.workers import Worker, WorkerSupervisor
from solalpha.signal.kill_switch import KillSwitch
from solalpha.signal.mode_manager import ModeManager
from solalpha.signal.pipeline import SignalPipeline
from solalpha.signal.risk_engine import RiskEngine
from solalpha.signal.smart_wallet_scorer import SmartWalletScorer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Protocol

    from solalpha.domain import ModeStr
    from solalpha.foundation.config import AppConfig

    class _HasRun(Protocol):
        """Anything with an async ``run()`` -- the optional worker components."""

        def run(self) -> Awaitable[None]: ...


_log = get_logger(__name__)


def _derive_ws_urls(http_urls: list[str], explicit_ws: list[str]) -> list[str]:
    """If no ws URLs given, derive them from https URLs (http -> ws, https -> wss)."""
    if explicit_ws:
        return list(explicit_ws)
    out: list[str] = []
    for u in http_urls:
        if u.startswith("https://"):
            out.append("wss://" + u[len("https://") :])
        elif u.startswith("http://"):
            out.append("ws://" + u[len("http://") :])
    return out


class _NamedWorker:
    """Tiny adapter so already-built components satisfy the `Worker` protocol."""

    def __init__(
        self,
        name: str,
        modes: tuple[ModeStr, ...],
        run_fn: Callable[[], Awaitable[None]],
    ) -> None:
        self.name = name
        self.modes = modes
        self._run = run_fn

    async def run(self) -> None:
        await self._run()


class Application:
    """The runtime root. Build once per process; call `run()` from `anyio.run`."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._clock = SystemClock()
        self._bus = Bus()
        self._store = SqliteStore(cfg.persistence.sqlite_path, clock=self._clock)
        self._parquet = ParquetStore(cfg.persistence.parquet_root, self._clock)
        self._health = HealthRegistry(self._clock)
        # Built in `run()` once stores are connected.
        self._kill: KillSwitch | None = None
        self._portfolio: PortfolioTracker | None = None
        self._snapshot_mgr: SnapshotManager | None = None
        self._mode_manager: ModeManager | None = None
        self._exporter: MetricsServer | None = None
        self._trade_log: TradeLog | None = None
        # Signal plane (built unconditionally; pipeline is a no-op until
        # NormalizedSwaps start arriving on the bus).
        self._scorer: SmartWalletScorer | None = None
        self._risk_engine: RiskEngine | None = None
        self._signal_pipeline: SignalPipeline | None = None
        # Data plane (only built when `rpc.urls` is non-empty).
        self._rpc: RpcPool | None = None
        self._dedupe: DedupeRing | None = None
        self._decoder_worker: DecoderWorker | None = None
        self._backfill: BackfillPoller | None = None
        self._ingestor: WebSocketIngestor | None = None
        self._smart_wallets: SmartWalletSubscriptionManager | None = None
        self._mint_cache: MintMetadataCache | None = None
        # Execution plane. PaperExecutor is always present; the live path is
        # only built when both the data plane and a configured keypair exist.
        self._paper_executor: PaperExecutor | None = None
        self._jupiter: JupiterClient | None = None
        self._raydium: RaydiumClient | None = None
        self._route_selector: RouteSelector | None = None
        self._alt_manager: AltManager | None = None
        self._tx_builder: TxBuilder | None = None
        self._live_executor: LiveExecutor | None = None
        self._stuck_resolver: StuckTxResolver | None = None
        self._execution_pipeline: ExecutionPipeline | None = None

    # ---- public ----

    async def run(self) -> None:
        """Run the application until SIGINT/SIGTERM. Idempotent on shutdown."""
        _log.info(
            "app_starting",
            profile=self._cfg.profile,
            mode=self._cfg.mode,
            live_eligible=self._cfg.is_live_eligible(),
            rpc_urls=len(self._cfg.rpc.urls),
        )
        await self._startup()
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._signal_handler, tg.cancel_scope)
                tg.start_soon(self._sighup_handler)
                supervisor = WorkerSupervisor(self._clock)
                workers = self._build_workers()
                tg.start_soon(supervisor.supervise, workers, self._current_mode)
        finally:
            # Shield shutdown so graceful cleanup (final snapshot, store
            # close, aiosqlite worker-thread join) always completes even
            # when `run()` was cancelled by SIGINT / a parent scope.
            with anyio.CancelScope(shield=True):
                await self._shutdown()

    # ---- lifecycle ----

    async def _startup(self) -> None:
        report = await recover(self._cfg)
        _log.info(
            "recovery_report",
            snapshot=report.snapshot_path,
            journal_replayed=report.journal_entries_replayed,
            warnings=len(report.warnings),
            fallback_used=report.fallback_used,
        )
        await self._store.connect()
        self._kill = KillSwitch(
            self._store,
            self._clock,
            self._cfg.kill_switch.file_path,
            poll_interval_s=self._cfg.kill_switch.poll_interval_s,
        )
        await self._kill.load()
        self._portfolio = PortfolioTracker(self._store, self._clock)
        await self._portfolio.load()
        self._trade_log = TradeLog(self._store, self._parquet, self._clock)
        self._snapshot_mgr = SnapshotManager(
            self._store,
            self._clock,
            self._cfg.persistence.snapshot_root,
            interval_s=self._cfg.persistence.snapshot_interval_s,
            retention_days=self._cfg.persistence.journal_retention_days,
        )
        self._mode_manager = ModeManager(
            self._cfg,
            self._bus,
            self._store,
            self._clock,
            self._kill,
            self._portfolio,
            self._health,
        )
        if self._cfg.metrics.enabled:
            self._exporter = MetricsServer(
                self._health,
                self._store,
                self._clock,
                host=self._cfg.metrics.host,
                port=self._cfg.metrics.port,
            )
        if self._cfg.rpc.urls:
            self._build_data_plane()
        self._scorer = SmartWalletScorer(
            self._store,
            self._clock,
            decay_half_life_days=self._cfg.smart_wallets.decay_half_life_days,
            min_score_smart=self._cfg.smart_wallets.min_score_to_subscribe,
        )
        await self._scorer.refresh()
        self._risk_engine = RiskEngine(
            self._cfg,
            self._store,
            self._clock,
            self._kill,
            self._portfolio,
            self._mode_manager,
            mint_cache=self._mint_cache,
        )
        self._signal_pipeline = SignalPipeline(
            self._cfg,
            self._bus,
            self._store,
            self._clock,
            self._scorer,
            self._mode_manager,
            self._risk_engine,
        )
        self._build_execution_plane()
        _log.info(
            "app_ready",
            data_plane=self._rpc is not None,
            signal_plane=True,
            execution_plane=self._execution_pipeline is not None,
            live_executor=self._live_executor is not None,
        )

    def _build_execution_plane(self) -> None:
        self._paper_executor = PaperExecutor(self._cfg, self._clock)
        assert self._mode_manager is not None
        assert self._portfolio is not None
        assert self._trade_log is not None
        if self._rpc is not None and self._cfg.keypair_path is not None:
            keypair_loader = KeypairLoader(self._cfg.keypair_path.get_secret_value())
            if keypair_loader.is_configured():
                self._jupiter = JupiterClient(
                    self._cfg.jupiter.base_url,
                    self._clock,
                    quote_timeout_s=self._cfg.jupiter.quote_timeout_s,
                    swap_timeout_s=self._cfg.jupiter.swap_timeout_s,
                )
                if self._cfg.raydium.enabled:
                    self._raydium = RaydiumClient(
                        self._cfg.raydium.base_url,
                        self._clock,
                        request_timeout_s=self._cfg.raydium.request_timeout_s,
                    )
                self._route_selector = RouteSelector(
                    self._cfg, self._mode_manager, self._jupiter, self._raydium
                )
                self._alt_manager = AltManager(self._rpc)
                self._tx_builder = TxBuilder(
                    self._rpc,
                    self._clock,
                    keypair_loader,
                    self._alt_manager,
                    compute_unit_limit=self._cfg.execution.compute_unit_limit,
                )
                self._live_executor = LiveExecutor(
                    self._cfg,
                    self._rpc,
                    self._store,
                    self._clock,
                    keypair_loader,
                    self._route_selector,
                    self._jupiter,
                    self._raydium,
                    self._tx_builder,
                    on_order_resolved=(
                        self._risk_engine.on_order_resolved
                        if self._risk_engine is not None
                        else None
                    ),
                )
                self._stuck_resolver = StuckTxResolver(
                    self._rpc,
                    self._store,
                    self._clock,
                    on_resolved=(
                        self._risk_engine.on_order_resolved
                        if self._risk_engine is not None
                        else None
                    ),
                )
        self._execution_pipeline = ExecutionPipeline(
            self._cfg,
            self._bus,
            self._clock,
            self._mode_manager,
            self._paper_executor,
            self._live_executor,
            self._portfolio,
            self._trade_log,
        )

    def _build_data_plane(self) -> None:
        self._rpc = RpcPool(
            self._cfg.rpc.urls,
            self._clock,
            request_timeout_s=self._cfg.rpc.request_timeout_s,
            health_quarantine_s=self._cfg.rpc.health_quarantine_s,
            health_window_s=self._cfg.rpc.health_window_s,
            health_min_success_rate=self._cfg.rpc.health_min_success_rate,
        )
        self._health.register("rpc_pool", self._rpc.probe)
        self._dedupe = DedupeRing()
        self._smart_wallets = SmartWalletSubscriptionManager(
            self._store,
            self._clock,
            max_subscriptions=self._cfg.smart_wallets.max_subscriptions,
            min_score=self._cfg.smart_wallets.min_score_to_subscribe,
        )
        self._decoder_worker = DecoderWorker(self._bus, self._rpc, self._clock)
        self._mint_cache = MintMetadataCache(self._store, self._rpc, self._clock)
        self._backfill = BackfillPoller(
            self._rpc,
            self._store,
            self._bus,
            self._dedupe,
            self._clock,
            addresses=self._smart_wallets.current(),
        )
        ws_urls = _derive_ws_urls(self._cfg.rpc.urls, self._cfg.rpc.ws_urls)
        if ws_urls:
            self._ingestor = WebSocketIngestor(
                ws_urls[0],
                self._bus,
                self._dedupe,
                self._clock,
                program_ids=tuple(DECODABLE_PROGRAMS),
                smart_wallets=self._smart_wallets.current(),
                heartbeat_s=self._cfg.rpc.ws_heartbeat_s,
                reconnect_max_s=self._cfg.rpc.ws_reconnect_max_s,
            )

    async def _shutdown(self) -> None:
        _log.info("app_stopping")
        if self._snapshot_mgr is not None:
            try:
                path = await self._snapshot_mgr.snapshot_now()
                _log.info("final_snapshot", path=str(path))
            except Exception as e:
                _log.warning("final_snapshot_failed", exc=str(e))
        await self._aclose_quietly("jupiter", self._jupiter)
        await self._aclose_quietly("raydium", self._raydium)
        await self._aclose_quietly("rpc", self._rpc)
        try:
            await self._store.close()
        except Exception as e:
            _log.warning("store_close_failed", exc=str(e))
        await self._bus.close()
        _log.info("app_stopped")

    async def _aclose_quietly(
        self, name: str, client: JupiterClient | RaydiumClient | RpcPool | None
    ) -> None:
        """Best-effort ``aclose()`` of an optional client during shutdown.

        A failure to close one client must not stop the others (or the store
        close + final snapshot) from running, so each close is guarded and
        logged rather than raised.
        """
        if client is None:
            return
        try:
            await client.aclose()
        except Exception as e:
            _log.warning(f"{name}_close_failed", exc=str(e))

    def _build_workers(self) -> list[Worker]:
        assert self._kill is not None
        assert self._snapshot_mgr is not None
        assert self._mode_manager is not None
        workers: list[Worker] = [
            _NamedWorker("kill_switch", (), self._kill.run),
            _NamedWorker("snapshot", (), self._snapshot_mgr.run),
            _NamedWorker("mode_manager", (), self._mode_manager.run),
        ]
        # Optional workers: each is appended when its component was built. Order
        # is immaterial -- the supervisor start_soons them all concurrently.
        optional: list[tuple[str, _HasRun | None]] = [
            ("metrics_server", self._exporter),
            ("smart_wallet_scorer", self._scorer),
            ("signal_pipeline", self._signal_pipeline),
            ("smart_wallet_subs", self._smart_wallets),
            ("decoder_worker", self._decoder_worker),
            ("backfill", self._backfill),
            ("ws_ingestor", self._ingestor),
            ("execution_pipeline", self._execution_pipeline),
            ("stuck_tx_resolver", self._stuck_resolver),
        ]
        for name, component in optional:
            if component is not None:
                workers.append(_NamedWorker(name, (), component.run))
        # rpc_reloader is gated on the data plane but runs its own method.
        if self._rpc is not None:
            workers.append(_NamedWorker("rpc_reloader", (), self._run_rpc_reloader))
        return workers

    def _current_mode(self) -> ModeStr:
        assert self._mode_manager is not None
        return self._mode_manager.mode

    async def _signal_handler(self, cancel_scope: anyio.CancelScope) -> None:
        try:
            with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
                async for sig in signals:
                    _log.info(
                        "shutdown_signal",
                        signal=sig.name if hasattr(sig, "name") else str(sig),
                    )
                    cancel_scope.cancel()
                    return
        except NotImplementedError:
            # Windows asyncio does not implement `add_signal_handler` on any
            # event loop, so anyio's signal receiver raises here on boot.
            # Ctrl+C still works: Python's default KeyboardInterrupt
            # propagates up through anyio.run and is caught in
            # `foundation/cli._run_app`, which lets `app.run`'s shielded
            # finally block take the clean shutdown path.
            _log.info("signal_handler_unavailable", reason="platform_asyncio_no_signal_handler")

    async def _sighup_handler(self) -> None:
        """Hot-reload the RPC endpoint set on POSIX SIGHUP.

        A no-op where SIGHUP is unavailable (e.g. Windows) -- the
        `solalpha reload-rpc` file trigger handled by `_run_rpc_reloader`
        works on every platform.
        """
        if not hasattr(signal, "SIGHUP"):
            return
        with anyio.open_signal_receiver(signal.SIGHUP) as sighups:
            async for _sig in sighups:
                _log.info("sighup_received")
                if self._rpc is None:
                    continue
                try:
                    from solalpha.foundation.config import load_config

                    fresh = load_config(profile=self._cfg.profile)
                    if fresh.rpc.urls:
                        count = self._rpc.reload(list(fresh.rpc.urls))
                        _log.info("rpc_reloaded", endpoints=count, trigger="sighup")
                    else:
                        _log.warning("sighup_reload_skipped", reason="no rpc.urls in config")
                except Exception as e:
                    _log.warning("sighup_reload_failed", exc=str(e), exc_type=type(e).__name__)

    async def _run_rpc_reloader(self) -> None:
        """Apply `solalpha reload-rpc` requests to the live `RpcPool`.

        The CLI writes the desired endpoint list to `<data_dir>/.reload-rpc`;
        this loop reads it, reconciles the pool, and deletes the request
        file. Cross-platform -- the SIGHUP path above is POSIX-only.
        """
        path = self._cfg.persistence.data_dir / ".reload-rpc"
        while True:
            try:
                if path.exists() and self._rpc is not None:
                    raw = path.read_text(encoding="utf-8")
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
                    urls = json.loads(raw)
                    if isinstance(urls, list) and urls:
                        count = self._rpc.reload([str(u) for u in urls])
                        _log.info("rpc_reloaded", endpoints=count, trigger="reload-rpc")
                    else:
                        _log.warning("rpc_reload_skipped", reason="empty or malformed request")
            except Exception as e:
                _log.warning("rpc_reload_failed", exc=str(e), exc_type=type(e).__name__)
            await self._clock.sleep(2.0)


__all__ = ["Application"]
