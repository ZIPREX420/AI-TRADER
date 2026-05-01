"""Safe write-back: research outputs always land in *_proposed.json or *_candidate.json.

Live execution path reads only canonical names. Promotion (rename to canonical) requires
gates passed AND operator approval (`approvals.json`) OR explicit auto-promote thresholds.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any


# All paths the research engine is allowed to write to. Anything else is rejected.
ALLOWED_PROPOSAL_FILES = {
    "data/research/proposed_lr_weights.json",
    "data/research/wallet_candidates.json",
    "data/research/promoted_patterns.json",
    "data/research/rejected_patterns.json",
    "data/research/precursors.json",
    "data/research/discovered_patterns.json",
    "data/research/anti_patterns.json",
    "data/research/approvals.json",
    "data/smart_wallets_candidate.json",
    "data/evolution_log.jsonl",
}

# Canonical (live) state files the research engine must NEVER write to:
FORBIDDEN_LIVE_FILES = {
    "data/lr_weights.json",
    "data/source_priors.json",
    "data/strategy_priors.json",
    "data/weights.json",
    "data/wallet_overrides.json",
    "data/disabled_sources.json",
    "data/params.json",
    "data/regime.json",
    "data/allocations.json",
    "data/risk_state.json",
    "data/smart_wallets.json",
    "data/smart_wallets_scored.json",
    "data/exec_params.json",
    "data/decay_state.json",
    "data/live_patterns.json",
}


def _is_allowed(path: Path) -> bool:
    norm = str(path).replace("\\", "/")
    if norm in FORBIDDEN_LIVE_FILES:
        return False
    return norm in ALLOWED_PROPOSAL_FILES


def write_proposal(path: Path, content: Any) -> Path:
    """Atomic write to a proposal-style path. Refuses canonical live paths."""
    norm = str(path).replace("\\", "/")
    if norm in FORBIDDEN_LIVE_FILES:
        raise PermissionError(f"refusing to write canonical live file: {path}")
    if norm not in ALLOWED_PROPOSAL_FILES:
        raise PermissionError(f"path not in ALLOWED_PROPOSAL_FILES: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if isinstance(content, (dict, list)):
        tmp.write_text(json.dumps(content, indent=2))
    else:
        tmp.write_text(str(content))
    os.replace(tmp, path)
    return path


def append_evolution_log(entry: dict) -> Path:
    p = Path("data/evolution_log.jsonl")
    norm = str(p).replace("\\", "/")
    if norm not in ALLOWED_PROPOSAL_FILES:
        raise PermissionError(f"evolution log path mis-registered: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": time.time(), **entry}) + "\n"
    with p.open("a") as f:
        f.write(line)
    return p


def fit_lr_weights(run_db_path: str, lambda_ridge: float = 0.5) -> dict:
    """Closed-form ridge logistic LS on (channels, outcome_binary).

    Channels read from trades.payload['channels'] when present; otherwise produces empty result.
    Returns dict {channel_name: weight}.
    """
    import sqlite3
    db = sqlite3.connect(run_db_path)
    try:
        rows = db.execute(
            "SELECT roi, payload FROM trades WHERE ts_close IS NOT NULL"
        ).fetchall()
    finally:
        db.close()
    if len(rows) < 30:
        return {}

    X: list[list[float]] = []
    y: list[float] = []
    channel_names: list[str] | None = None
    for roi, payload in rows:
        try:
            p = json.loads(payload) if payload else {}
        except Exception:
            continue
        ch = p.get("channels") or {}
        if not ch:
            continue
        if channel_names is None:
            channel_names = sorted(ch.keys())
        X.append([float(ch.get(k, 0.0)) for k in channel_names])
        y.append(1.0 if (roi is not None and float(roi) > 0) else 0.0)

    if not X or channel_names is None:
        return {}

    # Closed-form ridge: W = (X^T X + λI)^{-1} X^T (y_smoothed)
    n = len(X); d = len(channel_names)
    # Use numpy via duck-import to keep dep optional
    try:
        import numpy as np
    except ImportError:
        return {}
    Xn = np.array(X, dtype=float)
    yn = np.array(y, dtype=float)
    # Smooth y to logit space (Laplace): p = (k+1)/(n+2)
    p = (yn + 1.0) / 3.0
    z = np.log(p / (1 - p))
    A = Xn.T @ Xn + lambda_ridge * np.eye(d)
    b = Xn.T @ z
    w = np.linalg.solve(A, b)
    return {name: float(wi) for name, wi in zip(channel_names, w)}


def kl_divergence(p: dict, q: dict) -> float:
    """KL(p||q) on weight distributions (normalize positive parts; smoothing for robustness)."""
    keys = sorted(set(p) | set(q))
    eps = 1e-6
    pv = []; qv = []
    for k in keys:
        pv.append(max(float(p.get(k, 0.0)), 0.0) + eps)
        qv.append(max(float(q.get(k, 0.0)), 0.0) + eps)
    sp = sum(pv); sq = sum(qv)
    pv = [x / sp for x in pv]; qv = [x / sq for x in qv]
    import math as _m
    return sum(p_i * _m.log(p_i / q_i) for p_i, q_i in zip(pv, qv))
