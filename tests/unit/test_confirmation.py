from __future__ import annotations

from graph_sym_perf.confirm import evaluate_confirmation
from graph_sym_perf.core import ConfirmationConfig


def test_evaluate_confirmation_marks_threshold_supported_latency() -> None:
    record = evaluate_confirmation(
        "case_0001",
        (10.0, 11.0, 12.0),
        (210.0, 220.0, 230.0),
        ConfirmationConfig(min_latency_ratio=10.0, min_latency_delta_ms=50.0),
    )

    assert record.confirmed
    assert record.support_level == "latency_supported"
    assert record.reason == "LATENCY_ONLY_WEAK"
