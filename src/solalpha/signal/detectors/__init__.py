"""Signal-plane detectors.

Each detector watches the `NormalizedSwap` stream and emits
`DetectorSignal`s when its criterion is met. The combiner blends them.
"""

from __future__ import annotations

from solalpha.signal.detectors.base import Detector
from solalpha.signal.detectors.cluster import ClusterDetector
from solalpha.signal.detectors.flow_anomaly import FlowAnomalyDetector
from solalpha.signal.detectors.prepump import PrePumpDetector

__all__ = [
    "ClusterDetector",
    "Detector",
    "FlowAnomalyDetector",
    "PrePumpDetector",
]
