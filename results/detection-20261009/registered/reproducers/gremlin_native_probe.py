"""Native, same-anchor optimizer-rule probes; explicit --execute is required.

These inputs are not ordinary GraphSymPerf pattern candidates. A passing time
gate remains a signal pending native work/strategy inspection, and nothing is
written to BUG list or bugs.csv.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path
import random
from statistics import median
import sys
import uuid

WORKSPACE = next(parent for parent in Path(__file__).resolve().parents if (parent / "Src" / "executor.py").is_file())
sys.path.insert(0, str(WORKSPACE / "Src"))

from executor import (CaseFailure, _measure, _quantile, _result,
                      make_database, measurement_settings, summarize)
from main import read_config
from state import GraphSpec


class NativeS1(GraphSpec):
    """Only rename JanusGraph's reserved ``key`` property in this fixture."""

    def _groups(self):
        nodes, edges = super()._groups()
        nodes = [replace(group, properties={"gsp_probe_key" if key == "key" else key: value
                                            for key, value in group.properties.items()}) for group in nodes]
        return nodes, edges

    def state_checks(self):
        def rename(value):
            if isinstance(value, dict):
                return {"gsp_probe_key" if key == "key" else key: rename(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rename(item) for item in value]
            return value
        return rename(super().state_checks())


def open_native_database(database, settings, spec):
    db = make_database(database, settings)
    # The native probe owns this explicit fixture. Both adapter restore and
    # verification use its renamed properties; the shared repository stays
    # unchanged and the mapping is recorded next to the base graph recipe.
    db._recipe = lambda case: spec
    if database["dbms"] == "hugegraph":
        if not (database.get("dedicated") is True and database.get("allow_reset") is True
                and database.get("graph_binding") == "DEFAULT-hugegraph"):
            raise ValueError("native clear is restricted to the explicitly resettable dedicated HugeGraph fixture")
        def native_clear():
            endpoint = "/graphspaces/DEFAULT/graphs/hugegraph/clear?confirm_message=I%27m+sure+to+delete+all+data"
            db._connection.request("DELETE", endpoint, headers=db._headers)
            response = db._connection.getresponse()
            body = response.read()
            if response.status != 204:
                raise CaseFailure("environment_error", "native_huge_clear_failed:" + str(response.status),
                                  stop_target=True)
            db._native_reset_evidence = {"reset_method": "HugeGraph_1.7_graph_clear_API",
                                         "http_status": response.status, "response_bytes": len(body),
                                         "endpoint": endpoint,
                                         "source": "https://hugegraph.apache.org/versions/1.7/docs/clients/restful-api/graphs/"}
            if not db.health():
                raise CaseFailure("environment_error", "native_huge_clear_health_failed", stop_target=True)
        db._reset = native_clear
    return db


SOURCE_LINKS = {
    "janus_version": "https://raw.githubusercontent.com/JanusGraph/janusgraph/v1.1.0/pom.xml",
    "huge_version": "https://raw.githubusercontent.com/apache/hugegraph/1.7.0/hugegraph-server/pom.xml",
    "tp_373_ranking": "https://raw.githubusercontent.com/apache/tinkerpop/3.7.3/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/strategy/optimization/FilterRankingStrategy.java",
    "tp_373_inline": "https://raw.githubusercontent.com/apache/tinkerpop/3.7.3/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/strategy/optimization/InlineFilterStrategy.java",
    "tp_351_ranking": "https://raw.githubusercontent.com/apache/tinkerpop/3.5.1/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/strategy/optimization/FilterRankingStrategy.java",
    "tp_351_inline": "https://raw.githubusercontent.com/apache/tinkerpop/3.5.1/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/strategy/optimization/InlineFilterStrategy.java",
    "tp_373_count": "https://raw.githubusercontent.com/apache/tinkerpop/3.7.3/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/strategy/optimization/CountStrategy.java",
    "tp_351_count": "https://raw.githubusercontent.com/apache/tinkerpop/3.5.1/gremlin-core/src/main/java/org/apache/tinkerpop/gremlin/process/traversal/strategy/optimization/CountStrategy.java",
    "janus_local_range": "https://raw.githubusercontent.com/JanusGraph/janusgraph/v1.1.0/janusgraph-core/src/main/java/org/janusgraph/graphdb/tinkerpop/optimize/strategy/JanusGraphLocalQueryOptimizerStrategy.java",
    "janus_has_folder": "https://raw.githubusercontent.com/JanusGraph/janusgraph/v1.1.0/janusgraph-core/src/main/java/org/janusgraph/graphdb/tinkerpop/optimize/step/HasStepFolder.java",
    "janus_unique_adjacency": "https://raw.githubusercontent.com/JanusGraph/janusgraph/v1.1.0/janusgraph-core/src/main/java/org/janusgraph/graphdb/tinkerpop/optimize/strategy/AdjacentVertexHasUniquePropertyOptimizerStrategy.java",
}

ROOT = "g.V().hasLabel('ScorRoot').has('id',1)"
OUTPUT = ".project('uid').by(__.values('uid'))"
EXPENSIVE = ".filter(__.out('GSP_SC_HIGH').has('mark',false))"
PROBES = {
    "filter-wrapper": {
        "a": ROOT + EXPENSIVE + ".filter(__.has('id',0))" + OUTPUT,
        "b": ROOT + EXPENSIVE + ".has('id',0)" + OUTPUT,
        "expected_rows": [],
        "equivalence": "filter(has(id,0)) is the same pure vertex filter as has(id,0); both inputs retain the same written order and indexed anchor; the SINGLE id=1 anchor cannot also satisfy id=0",
        "work_obligation": "after the wrapper is inlined, the existing cheaper HasStep should be moved before the expensive traversal filter; inspect strategy order and downstream vertex access",
    },
    "count-nonnull": {
        "a": ROOT + ".filter(__.outE('GSP_SC_HIGH').count().is(P.neq(null)).is(P.gt(0)))" + OUTPUT,
        "b": ROOT + ".filter(__.outE('GSP_SC_HIGH').count().is(P.gt(0)))" + OUTPUT,
        "expected_rows": [{"uid": 1}],
        "equivalence": "count() always returns a non-null integer, including zero; its value is only tested inside this side-effect-free filter",
        "work_obligation": "the redundant non-null test should not hide count>0 from the existing CountStrategy; B should stop after finding the first matching edge",
    },
    "local-limit": {
        "a": ROOT + ".filter(__.outE('GSP_SC_HIGH').limit(1))" + OUTPUT,
        "b": ROOT + ".filter(__.local(__.outE('GSP_SC_HIGH').limit(1)))" + OUTPUT,
        "expected_rows": [{"uid": 1}],
        "equivalence": "filter supplies one root to its side-effect-free child; local is redundant around outE.limit(1), and only non-emptiness is observed",
        "work_obligation": "both forms have an explicit limit(1); compare the registered local/global child range-folding rules and backend slice limits",
    },
    "unique-singleton": {
        "a": ROOT + ".out('GSP_SC_HIGH').has('_gsp_identity',P.within(target_key))" + OUTPUT,
        "b": ROOT + ".out('GSP_SC_HIGH').has('_gsp_identity',P.eq(target_key))" + OUTPUT,
        "expected_rows": None,
        "equivalence": "a SINGLE String property compared to the same one-element within predicate or equality has identical matches and edge multiplicity",
        "work_obligation": "with optimizer-backend-access enabled and a matching ENABLED unique composite index, a singleton String predicate can identify the adjacent vertex just as equality can",
    },
}


def write(path, record):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(path)


def diagnostics(db, case, record):
    record["native_diagnostics"] = {}
    for side in ("a", "b"):
        pair = record["native_diagnostics"][side] = {}
        for label, analyze in (("strategy_explanation", False), ("profile_work", True)):
            try:
                pair[label] = db.explain(case[f"query_{side}"], case.get("parameters", {}), analyze=analyze)
            except Exception as exc:
                pair[label] = {"unavailable": type(exc).__name__, "reason": str(exc),
                               "details": getattr(exc, "details", {})}


def validate_pair(db, case, settings):
    results, baseline = {}, None
    for side in ("a", "b"):
        result, bag = _result(db, case[f"query_{side}"], case.get("parameters", {}), settings)
        results[side] = result["rows"]
        if baseline is None:
            baseline = bag
        elif bag != baseline:
            raise CaseFailure("result_mismatch", "native_probe_results_differ")
    from executor import typed_bag
    if baseline != typed_bag(case["expected_rows"]):
        raise CaseFailure("reference_mismatch", "native_probe_differs_from_constructed_result")
    return baseline, results


def probe(config, mechanism, scale, seed, directory, *, screen_only=False):
    rename_key = config["database"]["dbms"] == "janusgraph"
    spec = (NativeS1 if rename_key else GraphSpec)("S1", scale, seed)
    selected = PROBES[mechanism]
    case = {"seed": seed, "graph_spec": spec.to_dict(), "query_a": selected["a"],
            "query_b": selected["b"], "parameters": {}, "expected_rows": selected["expected_rows"]}
    if mechanism == "unique-singleton":
        if config["database"]["dbms"] != "janusgraph":
            raise ValueError("the unique-adjacency probe is specific to the inspected JanusGraph strategy")
        target = next(row for row in spec.iter_nodes() if row["labels"] == ["ScorTarget"])
        case["parameters"] = {"target_key": "ScorTarget:" + str(target["uid"])}
        case["expected_rows"] = [{"uid": target["uid"]}]
    settings = measurement_settings({**config["measurement"], "discovery_warmups": 3,
                                     "discovery_pairs": 6, "confirmation_sessions": 2,
                                     "confirmation_warmups": 3, "confirmation_pairs": 20,
                                     "control_pairs": 10, "ratio": 5.0, "ci_low": 5.0,
                                     "min_delta_ms": 100.0, "bootstrap_samples": 10000})
    record = {"dbms": config["database"]["dbms"], "language": "gremlin", "mechanism": mechanism,
              "status": "started", "attribution": "native_optimizer_rule_probe",
              "pattern_id": None, "graph_recipe_pattern": "S1", "graph_spec": spec.to_dict(),
              "native_schema_mapping": {"key": "gsp_probe_key"} if rename_key else {}, "native_schema": spec.schema(),
              "queries": {"a": selected["a"], "b": selected["b"]}, "parameters": case["parameters"],
              "expected_rows": case["expected_rows"], "settings": settings,
              "equivalence": selected["equivalence"], "work_obligation": selected["work_obligation"],
              "sources": SOURCE_LINKS, "confirmation_sessions": []}
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / "probe.json"
    write(artifact, record)
    db = None
    try:
        db = open_native_database(config["database"], settings, spec)
        record["database_version"] = db.version
        record["phase"] = "discovery_restore"
        write(artifact, record)
        db.restore(case)
        record["reset_evidence"] = getattr(db, "_native_reset_evidence", {"reset_method": "bounded_data_deletion"})
        record["state_evidence"] = db.check_state(case)
        record["phase"] = "discovery_measurement"
        write(artifact, record)
        if mechanism == "unique-singleton":
            values, _ = db._script("_gsp_graph.tx().rollback(); def m=_gsp_graph.openManagement(); def i=m.getGraphIndex('gsp_internal_identity'); "
                                   "def p=m.getPropertyKey('_gsp_identity'); def e=[optimizer_backend_access:_gsp_graph.getConfiguration().optimizerBackendAccess(), "
                                   "unique:i!=null && i.isUnique(), composite:i!=null && i.isCompositeIndex(), "
                                   "keys:i==null ? [] : i.getFieldKeys().collect{it.name()}, status:i==null ? [] : i.getFieldKeys().collect{i.getIndexStatus(it).toString()}, "
                                   "property_type:p==null ? null : p.dataType().getName(), cardinality:p==null ? null : p.cardinality().toString()]; m.rollback(); [e]")
            record["unique_index_evidence"] = values
            if (len(values) != 1 or values[0].get("optimizer_backend_access") is not True or
                    values[0].get("unique") is not True or values[0].get("composite") is not True or
                    values[0].get("keys") != ["_gsp_identity"] or values[0].get("status") != ["ENABLED"] or
                    values[0].get("property_type") != "java.lang.String" or values[0].get("cardinality") != "SINGLE"):
                raise CaseFailure("configuration_error", "unique_probe_strategy_preconditions_not_met")
        baseline, results = validate_pair(db, case, settings)
        discovery = record["discovery"] = {"warmups": [], "samples": [], "logical_rows": results}
        rng = random.Random(seed)
        _measure(db, case, baseline, settings, rng, 3, discovery["warmups"])
        _measure(db, case, baseline, settings, rng, 6, discovery["samples"])
        slow = "a" if median(row["a_ms"] for row in discovery["samples"]) >= median(row["b_ms"] for row in discovery["samples"]) else "b"
        discovery["summary"] = summarize(discovery["samples"], slow)
        strong = (discovery["summary"]["median_ratio"] >= 5 and
                  discovery["summary"]["median_delta_ms"] > 100 and
                  discovery["summary"]["direction_consistency"] >= settings["direction_consistency"])
        diagnostics(db, case, discovery)
        write(artifact, record)
        db.close()
        db = None
        if not strong or screen_only:
            record["status"] = "screen_signal_unconfirmed" if strong else "no_screen_signal"
            return record
        pooled, noise = [], []
        for number in range(2):
            db = open_native_database(config["database"], settings, spec)
            record["phase"] = "confirmation_" + str(number + 1) + "_restore"
            write(artifact, record)
            db.restore(case)
            session = {"number": number + 1, "state_evidence": db.check_state(case),
                       "reset_evidence": getattr(db, "_native_reset_evidence", {"reset_method": "bounded_data_deletion"}),
                       "warmups": [], "samples": [], "control_a": [], "control_b": []}
            record["confirmation_sessions"].append(session)
            record["phase"] = "confirmation_" + str(number + 1) + "_measurement"
            write(artifact, record)
            fresh, session["logical_rows"] = validate_pair(db, case, settings)
            if fresh != baseline:
                raise CaseFailure("result_mismatch", "native_probe_result_changed_after_rebuild")
            rng = random.Random(seed + 104729 * (number + 1))
            _measure(db, case, baseline, settings, rng, 3, session["warmups"])
            _measure(db, case, baseline, settings, rng, 20, session["samples"])
            for side in ("a", "b"):
                query = case[f"query_{side}"]
                _measure(db, case, baseline, settings, rng, 10, session[f"control_{side}"], queries=(query, query))
            session["summary"] = summarize(session["samples"], slow, bootstrap_samples=10000, seed=seed + number + 1)
            session["noise_p99_ms"] = max(_quantile([abs(row["a_ms"] - row["b_ms"]) for row in session[f"control_{side}"]], 0.99)
                                          for side in ("a", "b"))
            pooled.extend(session["samples"])
            noise.append(session["noise_p99_ms"])
            diagnostics(db, case, session)
            write(artifact, record)
            db.close()
            db = None
        summary = record["summary"] = summarize(pooled, slow, bootstrap_samples=10000, seed=seed)
        summary["noise_p99_ms"] = max(noise)
        stable = all(session["summary"]["median_ratio"] >= 5 and session["summary"]["ratio_ci_low"] >= 5 and
                     session["summary"]["median_delta_ms"] > 100 and
                     session["summary"]["median_delta_ms"] > session["noise_p99_ms"] and
                     session["summary"]["direction_consistency"] >= settings["direction_consistency"]
                     for session in record["confirmation_sessions"])
        passed = (stable and summary["median_ratio"] >= 5 and summary["ratio_ci_low"] >= 5 and
                  summary["median_delta_ms"] > 100 and summary["median_delta_ms"] > summary["noise_p99_ms"])
        record["status"] = "native_probe_signal_requires_plan_review" if passed else "native_probe_not_confirmed"
        record["passed_time_gate"] = passed
        record["phase"] = "completed"
        return record
    except Exception as exc:
        record.update(status="native_probe_failed", failure=type(exc).__name__, reason=str(exc),
                      details=getattr(exc, "details", {}))
        return record
    finally:
        if db is not None:
            db.close()
        write(artifact, record)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scale", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--mechanism", choices=("all", *PROBES), default="all")
    parser.add_argument("--screen-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    config = read_config(args.config)
    target = config["database"]["dbms"]
    if target not in {"janusgraph", "hugegraph"} or config["database"]["language"] != "gremlin":
        raise ValueError("this probe requires a configured dedicated JanusGraph or HugeGraph target")
    GraphSpec("S1", args.scale, args.seed)
    mechanisms = list(PROBES) if args.mechanism == "all" else [args.mechanism]
    if not args.execute:
        print(json.dumps({"dry_run": True, "dbms": target, "scale": args.scale,
                          "probes": {key: PROBES[key] for key in mechanisms}}, indent=2))
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    destination = WORKSPACE / "results" / "detection-20261009" / target / ("native-" + stamp)
    failed = False
    for mechanism in mechanisms:
        result = probe(config, mechanism, args.scale, args.seed, destination / mechanism, screen_only=args.screen_only)
        print(json.dumps({"mechanism": mechanism, "scale": args.scale, "status": result["status"],
                          "summary": result.get("summary") or result.get("discovery", {}).get("summary"),
                          "directory": str(destination / mechanism)}, ensure_ascii=False), flush=True)
        if result["status"] == "native_probe_failed":
            failed = True
            break
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
