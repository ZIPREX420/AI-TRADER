"""Critical safety: research engine must never write canonical live state files."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from pathlib import Path

import pytest

from src.research import integrate


def test_blocks_canonical_live_files(tmp_path, monkeypatch):
    # Monkey-patch Path resolution: just attempt the canonical path
    forbidden = Path("data/lr_weights.json")
    with pytest.raises(PermissionError):
        integrate.write_proposal(forbidden, {"w1": 0.4})


def test_blocks_unknown_paths(tmp_path):
    with pytest.raises(PermissionError):
        integrate.write_proposal(Path("data/research/some_random.json"), {"a": 1})


def test_allows_proposed_lr_weights(tmp_path, monkeypatch):
    # Run from a temp working dir so we don't pollute the real data/research
    monkeypatch.chdir(tmp_path)
    target = Path("data/research/proposed_lr_weights.json")
    integrate.write_proposal(target, {"w1": 0.4, "w2": 0.3})
    assert target.exists()
    data = json.loads(target.read_text())
    assert data["w1"] == 0.4


def test_kl_divergence_basic():
    p = {"a": 0.5, "b": 0.5}
    q = {"a": 0.5, "b": 0.5}
    assert integrate.kl_divergence(p, q) < 1e-6
    p2 = {"a": 0.9, "b": 0.1}
    assert integrate.kl_divergence(p2, q) > 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
