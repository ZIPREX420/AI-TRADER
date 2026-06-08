"""CLI commands added in Phase 8: report, mode set/show/clear, reload-rpc.

These drive the real `solalpha` Click app with `CliRunner` against a
hermetic temp config dir, so they exercise config loading, the SQLite
store, and the operator probe-file surfaces end to end.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from solalpha.foundation.cli import cli

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def _write_config(cfg_dir: Path, data_dir: Path, *, rpc_urls: list[str] | None = None) -> None:
    """Write a minimal `default.yaml` rooting persistence at `data_dir`."""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "logging:",
        "  level: error",  # keep stdout clean for JSON parsing
        "persistence:",
        f'  data_dir: "{data_dir.as_posix()}"',
        "metrics:",
        "  enabled: false",
    ]
    if rpc_urls:
        lines.append("rpc:")
        lines.append("  urls:")
        lines.extend(f'    - "{u}"' for u in rpc_urls)
    (cfg_dir / "default.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def cli_env(tmp_path: Path) -> tuple[Path, Path]:
    """A hermetic (config dir, data dir) pair for CLI invocations."""
    cfg_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    _write_config(cfg_dir, data_dir)
    return cfg_dir, data_dir


def test_report_on_empty_db(cli_env: tuple[Path, Path]) -> None:
    cfg_dir, _ = cli_env
    result = CliRunner().invoke(
        cli, ["--config-dir", str(cfg_dir), "report", "--day", "2026-05-20"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["day"] == "2026-05-20"
    assert data["fill_count"] == 0
    assert data["daily_pnl"] is None
    assert data["closed_positions"] == []


def test_mode_set_show_clear_roundtrip(cli_env: tuple[Path, Path]) -> None:
    cfg_dir, data_dir = cli_env
    runner = CliRunner()

    result = runner.invoke(
        cli, ["--config-dir", str(cfg_dir), "mode", "set", "PAPER", "--reason", "drill"]
    )
    assert result.exit_code == 0, result.output
    assert (data_dir / ".operator_mode").exists()

    result = runner.invoke(cli, ["--config-dir", str(cfg_dir), "mode", "show"])
    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["override"]["mode"] == "PAPER"
    assert shown["override"]["reason"] == "drill"

    result = runner.invoke(cli, ["--config-dir", str(cfg_dir), "mode", "clear"])
    assert result.exit_code == 0, result.output
    assert not (data_dir / ".operator_mode").exists()


def test_mode_show_when_unset(cli_env: tuple[Path, Path]) -> None:
    cfg_dir, _ = cli_env
    result = CliRunner().invoke(cli, ["--config-dir", str(cfg_dir), "mode", "show"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["override"] is None


def test_mode_set_rejects_non_paper(cli_env: tuple[Path, Path]) -> None:
    cfg_dir, _ = cli_env
    result = CliRunner().invoke(
        cli, ["--config-dir", str(cfg_dir), "mode", "set", "LIVE", "--reason", "x"]
    )
    # click.Choice only accepts PAPER -- a non-PAPER target is a usage error.
    assert result.exit_code != 0


def test_reload_rpc_refused_without_urls(cli_env: tuple[Path, Path]) -> None:
    cfg_dir, _ = cli_env
    result = CliRunner().invoke(
        cli,
        ["--config-dir", str(cfg_dir), "reload-rpc"],
        env={"SOLALPHA_RPC_URLS": ""},
    )
    # No endpoints configured -> refused with exit code 2.
    assert result.exit_code == 2


def test_reload_rpc_writes_request(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    _write_config(cfg_dir, data_dir, rpc_urls=["https://rpc.example.com"])
    result = CliRunner().invoke(
        cli,
        ["--config-dir", str(cfg_dir), "reload-rpc"],
        env={"SOLALPHA_RPC_URLS": ""},
    )
    assert result.exit_code == 0, result.output
    request = data_dir / ".reload-rpc"
    assert request.exists()
    assert json.loads(request.read_text(encoding="utf-8")) == ["https://rpc.example.com"]
