"""solalpha CLI — entrypoint for `solalpha` and `python -m solalpha`."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import click

from solalpha import __version__
from solalpha.foundation.config import AppConfig, load_config
from solalpha.foundation.errors import ConfigError, SolalphaError
from solalpha.foundation.logging import configure_logging, get_logger


def _bootstrap(config_dir: Path | None, profile: str | None) -> AppConfig:
    cfg = load_config(config_dir=config_dir, profile=profile)
    configure_logging(
        level=cfg.logging.level,
        fmt=cfg.logging.format,
        service="solalpha",
        version=__version__,
    )
    cfg.persistence.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.persistence.log_dir.mkdir(parents=True, exist_ok=True)
    cfg.persistence.parquet_root.mkdir(parents=True, exist_ok=True)
    cfg.persistence.snapshot_root.mkdir(parents=True, exist_ok=True)
    return cfg


def _bootstrap_ctx(ctx: click.Context, default_profile: str | None = None) -> AppConfig:
    """Build the ``AppConfig`` from the shared click context.

    Centralises the ``ctx.obj.get("config_dir") / ctx.obj.get("profile")``
    extraction that every command repeated. ``default_profile`` supplies the
    fallback used by the mode-specific commands (``paper`` / ``live`` /
    ``research``); the plain commands pass the context profile through as-is.
    """
    profile = ctx.obj.get("profile")
    if default_profile is not None:
        profile = profile or default_profile
    return _bootstrap(ctx.obj.get("config_dir"), profile)


@click.group(
    invoke_without_command=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Override ./config dir.",
)
@click.option("--profile", default=None, help="Override SOLALPHA_PROFILE.")
@click.version_option(version=__version__, prog_name="solalpha")
@click.pass_context
def cli(ctx: click.Context, config_dir: Path | None, profile: str | None) -> None:
    """solalpha — Solana alpha trading system."""
    ctx.ensure_object(dict)
    ctx.obj["config_dir"] = config_dir
    ctx.obj["profile"] = profile


@cli.command()
def version() -> None:
    """Print version."""
    click.echo(__version__)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Force PAPER mode regardless of profile.")
@click.pass_context
def run(ctx: click.Context, dry_run: bool) -> None:
    """Run the system with the configured profile."""
    cfg = _bootstrap_ctx(ctx)
    if dry_run:
        cfg = cfg.model_copy(update={"mode": "PAPER", "live_trading": False})
    _run_app(cfg)


@cli.command()
@click.pass_context
def paper(ctx: click.Context) -> None:
    """Run in PAPER mode."""
    cfg = _bootstrap_ctx(ctx, "paper")
    cfg = cfg.model_copy(update={"mode": "PAPER", "live_trading": False})
    _run_app(cfg)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Boot LIVE mode but block at first submit.")
@click.pass_context
def live(ctx: click.Context, dry_run: bool) -> None:
    """Run in LIVE mode (refuses without SOLALPHA_LIVE_TRADING=1)."""
    try:
        cfg = _bootstrap_ctx(ctx, "live")
    except ConfigError as e:
        click.echo(f"refused: {e}", err=True)
        sys.exit(2)
    if not cfg.is_live_eligible():
        click.echo(
            "refused: live mode requires SOLALPHA_LIVE_TRADING=1 (or alias SOLANA_LIVE_TRADING=1)",
            err=True,
        )
        sys.exit(2)
    cfg = cfg.model_copy(update={"mode": "LIVE", "live_trading": True})
    if dry_run:
        os.environ["SOLALPHA_DRY_RUN"] = "1"
    _run_app(cfg)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print last persisted health snapshot as JSON."""
    cfg = _bootstrap_ctx(ctx)

    async def fetch() -> dict[str, Any]:
        from solalpha.foundation.state import SqliteStore

        store = SqliteStore(cfg.persistence.sqlite_path)
        await store.connect()
        try:
            row = await store.fetch_one(
                "SELECT snapshot_json FROM health_snapshots ORDER BY id DESC LIMIT 1"
            )
            if row is None:
                return {"ts": None, "overall": "unknown", "note": "no snapshots persisted yet"}
            parsed = json.loads(str(row["snapshot_json"]))
            if isinstance(parsed, dict):
                return parsed
            return {"ts": None, "overall": "unknown", "note": "snapshot payload not an object"}
        finally:
            await store.close()

    snap = anyio.run(fetch)
    click.echo(json.dumps(snap, indent=2, sort_keys=True))


@cli.command()
@click.pass_context
def snapshot(ctx: click.Context) -> None:
    """Dump full state to a snapshot file."""
    cfg = _bootstrap_ctx(ctx)

    async def dump() -> str:
        from solalpha.foundation.clock import SystemClock
        from solalpha.foundation.state import SqliteStore
        from solalpha.observability.snapshot import SnapshotManager

        store = SqliteStore(cfg.persistence.sqlite_path)
        await store.connect()
        mgr = SnapshotManager(store, SystemClock(), cfg.persistence.snapshot_root)
        try:
            path = await mgr.snapshot_now()
            return str(path)
        finally:
            await store.close()

    p = anyio.run(dump)
    click.echo(p)


@cli.command()
@click.option("--day", default=None, help="UTC date YYYY-MM-DD (default: today).")
@click.pass_context
def report(ctx: click.Context, day: str | None) -> None:
    """Print the daily PnL + fill summary for a UTC day."""
    cfg = _bootstrap_ctx(ctx)
    target_day = day or datetime.now(UTC).strftime("%Y-%m-%d")

    async def go() -> dict[str, Any]:
        from solalpha.foundation.state import SqliteStore

        store = SqliteStore(cfg.persistence.sqlite_path)
        await store.connect()
        try:
            pnl = await store.fetch_one("SELECT * FROM daily_pnl WHERE day = ?", (target_day,))
            fills = await store.fetch_all(
                "SELECT f.fill_id, f.signature, f.usd_value, f.fee_lamports, "
                "f.priority_fee_lamports, o.mint, o.direction "
                "FROM fills f JOIN orders o ON o.order_id = f.order_id "
                "WHERE substr(f.block_time, 1, 10) = ? "
                "ORDER BY f.block_time",
                (target_day,),
            )
            closed = await store.fetch_all(
                "SELECT position_id, mint, realized_pnl_usd FROM positions "
                "WHERE state = 'closed' AND substr(closed_at, 1, 10) = ?",
                (target_day,),
            )
            return {
                "day": target_day,
                "daily_pnl": dict(pnl) if pnl else None,
                "fill_count": len(fills),
                "fills": [dict(r) for r in fills],
                "closed_positions": [dict(r) for r in closed],
            }
        finally:
            await store.close()

    click.echo(json.dumps(anyio.run(go), indent=2, sort_keys=True, default=str))


@cli.command("reload-rpc")
@click.pass_context
def reload_rpc(ctx: click.Context) -> None:
    """Signal a running instance to reload its RPC endpoint set.

    Reads the current RPC endpoints from config (YAML + SOLALPHA_RPC_URLS)
    and writes a reload request that a running `solalpha` process applies
    within a couple of seconds. On POSIX you may instead send SIGHUP.
    """
    cfg = _bootstrap_ctx(ctx)
    urls = list(cfg.rpc.urls)
    if not urls:
        click.echo(
            "refused: no rpc.urls configured (set SOLALPHA_RPC_URLS or config rpc.urls)",
            err=True,
        )
        sys.exit(2)
    path = cfg.persistence.data_dir / ".reload-rpc"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(urls), encoding="utf-8")
    click.echo(f"reload-rpc requested: {len(urls)} endpoint(s)")


@cli.group()
def kill() -> None:
    """Kill switch controls."""


@kill.command("arm")
@click.option("--reason", required=True, help="Why armed.")
@click.option("--by", default="cli", help="Operator name.")
@click.pass_context
def kill_arm(ctx: click.Context, reason: str, by: str) -> None:
    cfg = _bootstrap_ctx(ctx)

    async def go() -> None:
        from solalpha.foundation.state import SqliteStore

        store = SqliteStore(cfg.persistence.sqlite_path)
        await store.connect()
        try:
            now = datetime.now(UTC).isoformat()
            await store.execute(
                "UPDATE kill_switch SET armed=1, reason=?, since=?, by_who=? WHERE id=1",
                (reason, now, by),
            )
            cfg.kill_switch.file_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.kill_switch.file_path.touch(exist_ok=True)
        finally:
            await store.close()

    anyio.run(go)
    click.echo(f"kill switch armed: {reason}")


@kill.command("disarm")
@click.option("--by", default="cli", help="Operator name.")
@click.pass_context
def kill_disarm(ctx: click.Context, by: str) -> None:
    cfg = _bootstrap_ctx(ctx)

    async def go() -> None:
        from solalpha.foundation.state import SqliteStore

        store = SqliteStore(cfg.persistence.sqlite_path)
        await store.connect()
        try:
            await store.execute(
                "UPDATE kill_switch SET armed=0, reason=NULL, since=NULL, by_who=? WHERE id=1",
                (by,),
            )
            with contextlib.suppress(FileNotFoundError):
                cfg.kill_switch.file_path.unlink()
        finally:
            await store.close()

    anyio.run(go)
    click.echo("kill switch disarmed")


@cli.group()
def mode() -> None:
    """Operator runtime-mode controls."""


@mode.command("set")
@click.argument("target", type=click.Choice(["PAPER"], case_sensitive=False))
@click.option("--reason", required=True, help="Why the override is set.")
@click.option("--by", default="cli", help="Operator name.")
@click.pass_context
def mode_set(ctx: click.Context, target: str, reason: str, by: str) -> None:
    """Pin the runtime mode (operator override).

    Only PAPER may be set: it stops live execution without a HALT. LIVE and
    DEGRADED_* are health-driven; use `solalpha kill arm` to force HALT.
    A running instance applies the override within ~1 s.
    """
    cfg = _bootstrap_ctx(ctx)
    path = cfg.persistence.data_dir / ".operator_mode"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": target.upper(),
        "reason": reason,
        "by": by,
        "since": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    click.echo(f"operator mode override set: {target.upper()} ({reason})")


@mode.command("clear")
@click.pass_context
def mode_clear(ctx: click.Context) -> None:
    """Remove the operator override; hand control back to the health gate."""
    cfg = _bootstrap_ctx(ctx)
    path = cfg.persistence.data_dir / ".operator_mode"
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    click.echo("operator mode override cleared")


@mode.command("show")
@click.pass_context
def mode_show(ctx: click.Context) -> None:
    """Print the current operator override, if any."""
    cfg = _bootstrap_ctx(ctx)
    path = cfg.persistence.data_dir / ".operator_mode"
    if not path.exists():
        click.echo(json.dumps({"override": None}, indent=2, sort_keys=True))
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        click.echo(f"operator override file unreadable: {e}", err=True)
        sys.exit(1)
    click.echo(json.dumps({"override": data}, indent=2, sort_keys=True))


@cli.group()
def research() -> None:
    """Research / backfill commands."""


@research.command("backfill")
@click.option("--since", required=True, help="ISO date or RFC3339 datetime.")
@click.option("--until", default=None, help="Optional end (default: now).")
@click.pass_context
def research_backfill(ctx: click.Context, since: str, until: str | None) -> None:
    cfg = _bootstrap_ctx(ctx, "research")

    async def go() -> None:
        from solalpha.research.backfill import run_backfill

        await run_backfill(cfg, since=since, until=until)

    anyio.run(go)
    click.echo("backfill complete")


@research.command("replay")
@click.argument("session", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def research_replay(ctx: click.Context, session: Path) -> None:
    cfg = _bootstrap_ctx(ctx, "research")

    async def go() -> dict[str, Any]:
        from solalpha.research.replay import replay_session

        return await replay_session(cfg, session)

    result = anyio.run(go)
    click.echo(json.dumps(result, indent=2, default=str, sort_keys=True))


@research.command("walkforward")
@click.pass_context
def research_walkforward(ctx: click.Context) -> None:
    cfg = _bootstrap_ctx(ctx, "research")

    async def go() -> dict[str, Any]:
        from solalpha.research.replay import run_walkforward

        return await run_walkforward(cfg)

    result = anyio.run(go)
    click.echo(json.dumps(result, indent=2, default=str, sort_keys=True))


@cli.command()
@click.option("--snapshot", "snap_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--reconcile-stuck", is_flag=True, help="Re-poll stuck signatures.")
@click.option("--reconcile-positions", is_flag=True, help="Verify positions vs RPC balances.")
@click.pass_context
def recover(
    ctx: click.Context,
    snap_path: Path | None,
    reconcile_stuck: bool,
    reconcile_positions: bool,
) -> None:
    """Recover from snapshot + journal."""
    cfg = _bootstrap_ctx(ctx)

    async def go() -> dict[str, Any]:
        from solalpha.observability.recovery import recover as do_recover

        report = await do_recover(
            cfg,
            snapshot_path=snap_path,
            reconcile_stuck=reconcile_stuck,
            reconcile_positions=reconcile_positions,
        )
        return report.model_dump(mode="json")

    report = anyio.run(go)
    click.echo(json.dumps(report, indent=2, sort_keys=True))


def _run_app(cfg: AppConfig) -> None:
    log = get_logger(__name__)
    log.info("starting", profile=cfg.profile, mode=cfg.mode, version=__version__)
    try:
        from solalpha.runtime.app import Application

        app = Application(cfg)
        anyio.run(app.run)
    except SolalphaError as e:
        log.error("fatal", exc=str(e), exc_type=type(e).__name__)
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception as e:
        log.exception("unhandled", exc=str(e), exc_type=type(e).__name__)
        sys.exit(1)


def main() -> None:
    """Entrypoint exposed via console_scripts and `python -m solalpha`."""
    cli(obj={})


__all__ = ["cli", "main"]
