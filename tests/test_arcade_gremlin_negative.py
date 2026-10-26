"""Opt-in corruption checks for existing ArcadeDB Gremlin state contracts."""

import json
import os
from pathlib import Path

import pytest

from executor import CaseFailure, _gremlin_state_checks, make_database, typed_bag
from main import prepare_case
from state import GraphSpec
from test_arcade_gremlin_integration import arcade_config, _write


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("GRAPHSYMPERF_ARCADE_GREMLIN_TEST_CONFIG"),
    reason="no dedicated ArcadeDB Gremlin interface-test configuration supplied")]


@pytest.fixture(scope="module")
def negative_evidence_directory(tmp_path_factory):
    supplied = os.getenv("GRAPHSYMPERF_ARCADE_GREMLIN_TEST_OUTPUT")
    root = Path(supplied) if supplied else tmp_path_factory.mktemp("arcade-gremlin-negative")
    output = root / "negative-state-checks"
    output.mkdir(parents=True, exist_ok=True)
    yield output
    entries = [{"file": path.name, **{key: value for key, value in json.loads(path.read_text()).items()
                                     if key in {"status", "reason", "corruption"}}}
               for path in sorted(output.glob("negative-*.json"))]
    _write(output / "validation-summary.json", {"kind": "existing_state_corruption_checks",
                                                "planned": 3, "entries": entries})


@pytest.mark.parametrize("pattern,corruption", [
    ("P2", "wrong_inverse_pair_key"), ("P2", "missing_reverse_edge"), ("P4", "missing_reference_edge"),
])
def test_existing_state_checks_reject_corrupted_graph(arcade_config, negative_evidence_directory, pattern, corruption):
    case = prepare_case(None, pattern, 3, 17, 0, database=arcade_config["database"])
    assert "compile_error" not in case, case.get("compile_error")
    spec = GraphSpec.from_dict(case["graph_spec"])
    checks = [check for check in spec.state_checks()
              if check["kind"] == ("inverse_edges" if pattern == "P2" else "reference_edges")]
    assert checks, (pattern, spec.state_checks())
    rendered = _gremlin_state_checks(checks, lambda_free=True)
    path = negative_evidence_directory / f"negative-{pattern}-{corruption}.json"
    record = {"kind": "existing_state_corruption_check", "status": "started", "pattern": pattern,
              "corruption": corruption, "graph_spec": case["graph_spec"], "witness_checks": []}
    db = make_database(arcade_config["database"], arcade_config["measurement"])
    loaded = False
    restore_allowed = True
    try:
        record["version"] = db.version
        db.restore(case)
        loaded = True
        record["initial_state"] = db.check_state(case)
        if pattern == "P2":
            mutation = f"g.E().hasLabel('{checks[0]['reverse']}').limit(1)"
            if corruption == "wrong_inverse_pair_key":
                mutation += f".property('{checks[0]['pair_key']}',-1L).count().project('changed').by(__.identity())"
            else:
                mutation += ".drop()"
        else:
            mutation = f"g.E().hasLabel('{checks[0]['reference']}').limit(1).drop()"
        record["mutation"] = mutation
        record["mutation_response"] = db._query(
            mutation, {}, arcade_config["measurement"]["timeout_seconds"], command=True)

        # Test the relationship check itself, not only the population count
        # that also fails when one edge has been deleted.
        violations = 0
        for check in rendered:
            actual = db.execute(check["query"], {}, arcade_config["measurement"]["timeout_seconds"])
            record["witness_checks"].append({"query": check["query"], "expected_rows": check["expected_rows"],
                                             "execution": actual})
            assert actual["complete"], actual
            violations += typed_bag(actual["rows"]) != typed_bag(check["expected_rows"])
        assert violations > 0, record
        with pytest.raises(CaseFailure) as rejected:
            db.check_state(case)
        assert rejected.value.status == "cost_check_failed", rejected.value.status
        record["rejection"] = {"status": rejected.value.status, "reason": rejected.value.reason}
        db.restore(case)
        record["restored_state"] = db.check_state(case)
        record["status"] = "passed"
    except Exception as exc:
        record.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, CaseFailure) and exc.stop_target:
            # An unacknowledged timeout or failed transport may leave the
            # mutation running. Cleanup must not submit another graph write.
            restore_allowed = False
            record["restore_skipped"] = "target_execution_state_unknown"
        raise
    finally:
        if loaded and restore_allowed and record["status"] != "passed":
            try:
                db.restore(case)
                record["restored_after_failure"] = db.check_state(case)
            except Exception as exc:
                record["restore_error"] = f"{type(exc).__name__}: {exc}"
        db.close()
        _write(path, record)
