"""Historical input identity and diagnostic transport failures at runtime."""

import hashlib
import json

import pytest

import executor
import main
from ir import to_data
from languages import render_saved
from query import parse_query


def historical_age_case():
    # This is the observed S9 guard shape. Older archives classified only
    # function_coalesce, before its boolean/agtype support limit was known.
    trees = {
        "a": parse_query('MATCH (u:A) WHERE coalesce((NOT (u.key = 1)), null) MATCH (u)-[:R]->(v:B) RETURN 1 AS value'),
        "b": parse_query('MATCH (u:A) WHERE coalesce((NOT (u.key = 1)), null) RETURN 1 AS value'),
    }
    case = {"pattern": "S9", "seed": 0, "scale": 3, "setup": [], "checks": [],
            "parameters": {}, "expected_rows": [], "target_dbms": "age", "language": "cypher",
            "query_ir": {side: to_data(tree) for side, tree in trees.items()},
            "output_columns": {}, "required_features": {}}
    for side, tree in trees.items():
        saved = render_saved(tree, "age")
        case[f"query_{side}"] = saved.text
        case["output_columns"][side] = [list(item) for item in saved.columns]
        case["required_features"][side] = sorted(saved.required_features - {"boolean_expression_coalesce"})
    logical = {key: case.get(key) for key in ("query_ir", "graph_spec", "parameters", "expected_rows")}
    case["ir_hash"] = hashlib.sha256(json.dumps(logical, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return case


def test_same_target_replay_preserves_historical_input_hash_and_checks_current_gate(tmp_path, monkeypatch):
    original = tmp_path / "historical"
    case = historical_age_case()
    main.save_case(original, case, {}, {"status": "query_error", "reason": "42804"}, format_version=3)
    before_bytes = (original / "result.json").read_bytes()
    before = json.loads(before_bytes)
    assert main.read_case(original)[0] == case
    assert "boolean_expression_coalesce" not in case["required_features"]["a"]

    def unexpected_connection(*args, **kwargs):
        pytest.fail("current support gate must run before creating a database")

    monkeypatch.setattr(executor, "make_database", unexpected_connection)
    config = {"database": {"dbms": "age", "language": "cypher"}, "output": str(tmp_path / "replays")}
    assert main.replay(original, config) == 0
    after_path = next((tmp_path / "replays").rglob("result.json"))
    after = json.loads(after_path.read_text())
    assert after["format_version"] == 3
    assert after["input_hash"] == before["input_hash"]
    assert after["case"] == before["case"]
    assert after["result"]["status"] == "unsupported_feature"
    assert "boolean_expression_coalesce" in after["result"]["reason"]
    assert after["result"]["failure_phase"] == "configuration"
    assert not after["result"]["confirmation_sessions"]
    assert (original / "result.json").read_bytes() == before_bytes


class ServiceUnavailable(Exception):
    pass


class CodedTransactionTimeout(Exception):
    code = "Neo.TransientError.Transaction.TransactionTimedOut"


@pytest.mark.parametrize("failure", [TimeoutError("profile timed out"), ServiceUnavailable("connection lost"),
                                      CodedTransactionTimeout("diagnostic transaction timed out")])
def test_profile_transport_failure_stops_following_queries_and_clears_state_cache(monkeypatch, failure):
    case = {"seed": 7, "setup": [], "checks": [], "parameters": {},
            "query_a": 'MATCH (n:A) WHERE n.uid = 1 AND true RETURN n.uid',
            "query_b": 'MATCH (n:A) WHERE true AND n.uid = 1 RETURN n.uid',
            "expected_rows": [{"n.uid": 1}]}

    class Database:
        instances = []
        version = "unit-test"

        def __init__(self, config, settings):
            self.calls, self.plan_calls = [], []
            self.closed = False
            self.instances.append(self)

        def restore(self, state):
            self.state = state

        def health(self):
            return True

        def execute(self, text, parameters, timeout):
            self.calls.append(text)
            return {"rows": self.state["expected_rows"], "complete": True,
                    "elapsed_ms": 5.0 if text == self.state["query_b"] else 1.0}

        def explain(self, text, parameters, analyze=True):
            self.plan_calls.append(text)
            raise failure

        def close(self):
            self.closed = True

    monkeypatch.setattr(executor, "make_database", Database)
    cache = {}
    report = executor.run_case(case, {}, {"discovery_warmups": 0, "discovery_pairs": 2,
                                        "confirmation_warmups": 0, "confirmation_pairs": 4,
                                        "control_pairs": 2, "bootstrap_samples": 100}, state_cache=cache)
    assert report["status"] == "candidate"
    assert report["summary"]["median_ratio"] == 5
    assert report["stop_target"] is True
    assert len(Database.instances) == 3
    assert Database.instances[-1].plan_calls == [case["query_a"]]
    assert set(report["plans"]) == {"a"}
    assert report["plans"]["a"]["status"] == "unavailable"
    assert "cancellation_verified" not in report["plans"]["a"]
    assert cache == {}
    assert all(db.closed for db in Database.instances)
