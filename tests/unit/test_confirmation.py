from __future__ import annotations

from graph_sym_perf.confirm import evaluate_confirmation
from graph_sym_perf.core import ConfirmationConfig, ProfileRecord, TargetSystem


def test_evaluate_confirmation_does_not_upgrade_latency_without_profile_support() -> None:
    record = evaluate_confirmation(
        "case_0001",
        (10.0, 11.0, 12.0),
        (210.0, 220.0, 230.0),
        ConfirmationConfig(min_latency_ratio=10.0, min_latency_delta_ms=50.0),
    )

    assert not record.confirmed
    assert record.support_level == "plan_only"
    assert record.reason == "RUNTIME_WITHOUT_PROFILE_SUPPORT"


def test_evaluate_confirmation_marks_profile_and_runtime_supported_when_both_layers_pass() -> None:
    record = evaluate_confirmation(
        "case_0002",
        (10.0, 11.0, 12.0),
        (210.0, 220.0, 230.0),
        ConfirmationConfig(min_latency_ratio=10.0, min_latency_delta_ms=50.0, min_db_hits_ratio=2.0),
        original_profiles=(
            ProfileRecord(
                system=TargetSystem.NEO4J,
                query_kind="original",
                elapsed_ms=10.0,
                plan=None,
                metrics={"db_hits": 10.0},
            ),
        ),
        transformed_profiles=(
            ProfileRecord(
                system=TargetSystem.NEO4J,
                query_kind="transformed",
                elapsed_ms=220.0,
                plan=None,
                metrics={"db_hits": 100.0},
            ),
        ),
    )

    assert record.confirmed
    assert record.support_level == "profile_and_runtime_supported"
