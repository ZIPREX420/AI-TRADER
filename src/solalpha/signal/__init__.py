"""Signal + risk plane.

Phase 1 shipped `KillSwitch` and `ModeManager`. Phase 3 adds the rest:
detectors (prepump / cluster / flow_anomaly), the smart-wallet scorer, the
confidence combiner, the portfolio sizer, the hard risk engine, and the
signal pipeline worker.
"""

from __future__ import annotations

from solalpha.signal.combiner import ConfidenceCombiner
from solalpha.signal.detectors import (
    ClusterDetector,
    Detector,
    FlowAnomalyDetector,
    PrePumpDetector,
)
from solalpha.signal.kill_switch import KillSwitch
from solalpha.signal.mode_manager import ModeManager
from solalpha.signal.pipeline import SignalPipeline
from solalpha.signal.risk_engine import RiskEngine
from solalpha.signal.sizer import PortfolioSizer
from solalpha.signal.smart_wallet_scorer import SmartWalletScorer

__all__ = [
    "ClusterDetector",
    "ConfidenceCombiner",
    "Detector",
    "FlowAnomalyDetector",
    "KillSwitch",
    "ModeManager",
    "PortfolioSizer",
    "PrePumpDetector",
    "RiskEngine",
    "SignalPipeline",
    "SmartWalletScorer",
]
