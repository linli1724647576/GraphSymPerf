from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest

from graph_sym_perf.experiment.selection import (
    AffectedVersionRegressionReport,
    CellSummary,
    SelectionIntegrityError,
    evaluate_p0_gate,
    evaluate_p1_cell,
    load_pilot_selection,
    select_p2_cells,
    write_pilot_selection,
)


MANIFEST_HASH = "a" * 64
CAMPAIGN_ID = "campaign-fixture"


def _cell(
    rule_id: str = "constant_true_disjunction",
    family: str = "bool",
    graph_scale: int = 128,
    **changes: object,
) -> CellSummary:
    values: dict[str, object] = {
        "campaign_id": CAMPAIGN_ID,
        "phase": "p1",
        "input_manifest_hash": MANIFEST_HASH,
        "target": "neo4j",
        "rule_id": rule_id,
        "family": family,
        "stream": "discovery",
        "graph_scale": graph_scale,
        "shard_seed": graph_scale,
        "accepted_pairs": 200,
        "quota": 200,
        "applicability_count": 100,
        "stage2_count": 10,
        "validation_passed_count": 0,
        "control_validation_passed_count": 0,
        "validation_pair_yield": 0.0,
        "validation_yield_ci_low": 0.0,
        "best_validated_delta_ms": 0.0,
        "best_validated_ratio": 1.0,
        "best_ratio_ci_low": 1.0,
        "best_delta_over_noise": 0.0,
        "typed_mismatches": 0,
        "certificate_complete": True,
        "health_events": 0,
        "artifact_complete": True,
        "timeout_rate": 0.0,
        "terminal_state": "completed",
    }
    values.update(changes)
    return CellSummary(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("summary", "reason"),
    (
        (_cell(typed_mismatches=1), "typed_equivalence_mismatch"),
        (_cell(accepted_pairs=199), "underfilled"),
        (_cell(certificate_complete=False), "certificate_incomplete"),
        (_cell(health_events=1), "health_event"),
        (_cell(artifact_complete=False), "artifact_incomplete"),
        (_cell(terminal_state="resource_exhausted"), "resource_exhausted"),
        (_cell(terminal_state="infrastructure_failed"), "infrastructure_failed"),
    ),
)
def test_p1_hard_gate_is_fail_closed(summary: CellSummary, reason: str) -> None:
    decision = evaluate_p1_cell(summary)
    assert not decision.eligible
    assert reason in decision.reason_codes


def test_p0_gate_requires_core_control_and_regression_conditions() -> None:
    report = evaluate_p0_gate(
        core_cells=(replace(_cell(), phase="p0", accepted_pairs=50, quota=50),),
        control_cells=(
            replace(
                _cell("alpha_renaming_symmetry", "sym"),
                phase="p0",
                stream="control",
                accepted_pairs=50,
                quota=50,
            ),
        ),
        regression=AffectedVersionRegressionReport(passed=True, artifact_complete=True),
    )
    assert report.passed

    contaminated = evaluate_p0_gate(
        core_cells=(replace(_cell(), phase="p0", accepted_pairs=50, quota=50),),
        control_cells=(
            replace(
                _cell("alpha_renaming_symmetry", "sym"),
                phase="p0",
                stream="control",
                accepted_pairs=50,
                quota=50,
                control_validation_passed_count=1,
            ),
        ),
        regression=AffectedVersionRegressionReport(passed=True, artifact_complete=True),
    )
    assert not contaminated.passed
    assert "control_validation_pass" in contaminated.reason_codes


def _winner(rule_id: str, family: str, scale: int, score: float) -> CellSummary:
    return _cell(
        rule_id,
        family,
        scale,
        shard_seed=scale * 10 + len(rule_id),
        validation_passed_count=1,
        validation_pair_yield=score,
        validation_yield_ci_low=score,
        best_validated_delta_ms=score * 100,
        best_validated_ratio=1.0 + score,
        best_ratio_ci_low=1.0 + score / 2,
        best_delta_over_noise=score * 10,
    )


def test_selector_keeps_all_validation_winners_and_bounded_family_fallbacks() -> None:
    cells = (
        _winner("r1", "bool", 128, 0.9),
        _winner("r2", "sym", 128, 0.8),
        _cell("r3", "bool", 256, shard_seed=301, validation_yield_ci_low=0.7),
        _cell("r4", "bool", 512, shard_seed=401, validation_yield_ci_low=0.6),
        _cell("r5", "sym", 256, shard_seed=501, validation_yield_ci_low=0.5),
        _cell("r6", "sym", 512, shard_seed=601, validation_yield_ci_low=0.4),
    )
    selection = select_p2_cells(
        cells,
        max_cells=6,
        campaign_id=CAMPAIGN_ID,
        input_p1_manifest_hash=MANIFEST_HASH,
    )
    assert selection.status == "selected"
    assert {cell.rule_id for cell in selection.cells} >= {"r1", "r2"}
    assert len(selection.cells) <= 6
    assert sum(cell.family == "bool" and cell.validation_passed_count == 0 for cell in selection.cells) <= 1
    assert sum(cell.family == "sym" and cell.validation_passed_count == 0 for cell in selection.cells) <= 1


def test_two_scales_for_one_rule_require_independent_validation_passes() -> None:
    one_pass = (
        _winner("pattern_order_symmetry", "sym", 128, 0.8),
        _cell(
            "pattern_order_symmetry",
            "sym",
            512,
            shard_seed=999,
            validation_yield_ci_low=0.9,
        ),
    )
    selection = select_p2_cells(
        one_pass,
        campaign_id=CAMPAIGN_ID,
        input_p1_manifest_hash=MANIFEST_HASH,
    )
    assert len([cell for cell in selection.cells if cell.rule_id == "pattern_order_symmetry"]) == 1

    both_pass = tuple(replace(cell, validation_passed_count=1) for cell in one_pass)
    selection = select_p2_cells(
        both_pass,
        campaign_id=CAMPAIGN_ID,
        input_p1_manifest_hash=MANIFEST_HASH,
    )
    assert len([cell for cell in selection.cells if cell.rule_id == "pattern_order_symmetry"]) == 2


def test_selector_is_deterministic_under_shuffle_and_exact_ties() -> None:
    cells = [
        _cell("z_rule", "bool", 512, shard_seed=1),
        _cell("a_rule", "bool", 128, shard_seed=2),
        _cell("s_rule", "sym", 256, shard_seed=3),
    ]
    expected = select_p2_cells(
        cells,
        campaign_id=CAMPAIGN_ID,
        input_p1_manifest_hash=MANIFEST_HASH,
    )
    random.Random(42).shuffle(cells)
    actual = select_p2_cells(
        cells,
        campaign_id=CAMPAIGN_ID,
        input_p1_manifest_hash=MANIFEST_HASH,
    )
    assert actual == expected
    assert [(cell.rule_id, cell.graph_scale) for cell in actual.cells] == [
        ("a_rule", 128),
        ("s_rule", 256),
    ]


def test_more_than_six_validation_winners_fails_with_selection_overflow() -> None:
    cells = tuple(
        _winner(f"r{index}", "bool" if index % 2 else "sym", 128, 1.0 - index / 100)
        for index in range(7)
    )
    selection = select_p2_cells(
        cells,
        max_cells=6,
        campaign_id=CAMPAIGN_ID,
        input_p1_manifest_hash=MANIFEST_HASH,
    )
    assert selection.status == "selection_overflow"
    assert selection.cells == ()
    assert "selection_overflow" in selection.reason_codes


def test_control_contamination_fails_the_entire_selection() -> None:
    contaminated = replace(
        _cell("alpha_renaming_symmetry", "sym"),
        stream="control",
        control_validation_passed_count=1,
    )
    selection = select_p2_cells(
        (_winner("r1", "bool", 128, 0.5), contaminated),
        campaign_id=CAMPAIGN_ID,
        input_p1_manifest_hash=MANIFEST_HASH,
    )
    assert selection.status == "control_contamination"
    assert selection.cells == ()


def test_duplicate_cell_identity_and_cross_manifest_inputs_fail_closed() -> None:
    cell = _cell()
    with pytest.raises(SelectionIntegrityError, match="duplicate"):
        select_p2_cells(
            (cell, cell),
            campaign_id=CAMPAIGN_ID,
            input_p1_manifest_hash=MANIFEST_HASH,
        )
    with pytest.raises(SelectionIntegrityError, match="manifest"):
        select_p2_cells(
            (replace(cell, input_manifest_hash="b" * 64),),
            campaign_id=CAMPAIGN_ID,
            input_p1_manifest_hash=MANIFEST_HASH,
        )
    with pytest.raises(SelectionIntegrityError, match="campaign"):
        select_p2_cells(
            (replace(cell, campaign_id="other"),),
            campaign_id=CAMPAIGN_ID,
            input_p1_manifest_hash=MANIFEST_HASH,
        )
    with pytest.raises(SelectionIntegrityError, match="phase"):
        select_p2_cells(
            (replace(cell, phase="p0"),),
            campaign_id=CAMPAIGN_ID,
            input_p1_manifest_hash=MANIFEST_HASH,
        )


def test_pilot_selection_is_write_once_content_bound_and_context_checked(tmp_path) -> None:
    selection = select_p2_cells(
        (_winner("r1", "bool", 128, 0.5),),
        campaign_id=CAMPAIGN_ID,
        input_p1_manifest_hash=MANIFEST_HASH,
    )
    path = tmp_path / "pilot_selection.json"
    write_pilot_selection(selection, path)
    loaded = load_pilot_selection(
        path,
        expected_campaign_id=CAMPAIGN_ID,
        expected_p1_manifest_hash=MANIFEST_HASH,
    )
    assert loaded == selection
    with pytest.raises(FileExistsError):
        write_pilot_selection(selection, path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["selected_cells"][0]["graph_scale"] = 512
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SelectionIntegrityError, match="content hash"):
        load_pilot_selection(
            path,
            expected_campaign_id=CAMPAIGN_ID,
            expected_p1_manifest_hash=MANIFEST_HASH,
        )
