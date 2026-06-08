"""DBSCAN pattern miner over normalized-swap feature vectors."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, ConfigDict
from sklearn.cluster import DBSCAN

if TYPE_CHECKING:
    from collections.abc import Iterable

    from solalpha.domain import NormalizedSwap


class PatternCluster(BaseModel):
    model_config = ConfigDict(frozen=True)

    cluster_id: int
    size: int
    mints: tuple[str, ...]
    feature_centroid: tuple[float, ...]


def mine_patterns(
    swaps: Iterable[NormalizedSwap],
    *,
    eps: float = 0.5,
    min_samples: int = 5,
    pool_liquidity_usd: float = 25_000.0,
) -> list[PatternCluster]:
    swap_list = list(swaps)
    if not swap_list:
        return []
    features = np.array(
        [_feature_vector(s, pool_liquidity_usd) for s in swap_list],
        dtype=float,
    )
    if features.size == 0:
        return []
    db = DBSCAN(eps=eps, min_samples=min_samples)
    labels = db.fit_predict(features)
    clusters: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        if label == -1:
            continue
        clusters.setdefault(int(label), []).append(idx)
    out: list[PatternCluster] = []
    for label, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        centroid = features[members].mean(axis=0).tolist()
        mints = tuple({swap_list[i].mint for i in members})
        out.append(
            PatternCluster(
                cluster_id=label,
                size=len(members),
                mints=mints,
                feature_centroid=tuple(float(x) for x in centroid),
            )
        )
    return out


def _feature_vector(swap: NormalizedSwap, pool_liquidity_usd: float) -> list[float]:
    side = 1.0 if swap.side == "buy" else 0.0
    log_usd = math.log10(max(1.0, swap.usd_value))
    impact = min(1.0, swap.usd_value / max(1.0, pool_liquidity_usd))
    return [side, log_usd, impact]


__all__ = ["PatternCluster", "mine_patterns"]
