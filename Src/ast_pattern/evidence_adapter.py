"""Bind independently obtained runtime diagnostics to one immutable case."""

from __future__ import annotations

from . import METHOD_VERSION
from .contracts import StageFailure, digest


def runtime_evidence(context, *, graph_digest, query_digest, target_id):
    """Accept bound execution reports, without guessing physical AST mappings."""
    if not isinstance(context, dict):
        raise StageFailure("M6", "invalid_runtime_evidence", "Runtime context must be a bound report")
    if context.get("graph_digest") != graph_digest or context.get("query_digest") != query_digest:
        raise StageFailure("M6", "contract_drift", "Runtime evidence belongs to another graph or query pair")
    if context.get("target_id", target_id) != target_id:
        raise StageFailure("M6", "contract_drift", "Runtime evidence belongs to another target instance")
    report = context.get("report", {})
    if not isinstance(report, dict):
        raise StageFailure("M6", "invalid_runtime_evidence", "Execution evidence must contain a report mapping")
    result_relation = ("refuted" if report.get("status") in {"result_mismatch", "reference_mismatch"}
                       else "proved" if report.get("result_check") == "passed" else "unknown")
    return {"status": "observed" if result_relation == "proved" else "unverified",
            "result_relation": result_relation, "database_version": report.get("database_version", "unknown"),
            "execution_status": report.get("status", "unknown"), "graph_digest": graph_digest,
            "query_digest": query_digest, "target_id": target_id,
            "plans": report.get("plans", {}), "physical_plan_mapping": "unknown",
            "physical_invocations": "unknown", "timing_summary": report.get("summary"),
            "reason": "Raw plans do not establish a reliable mapping to this AST position"}


def validate_dependency_case(case, database=None):
    """Reject missing or stale M6 certificates before any new-case execution."""
    if case.get("method_version") != METHOD_VERSION:
        if case.get("method_version") is not None or "stage_records" in case or case.get("generation_method") == METHOD_VERSION:
            raise StageFailure("M6", "contract_drift", "Missing or unsupported AST method version")
        return
    from .pipeline import validate_stage_records
    try:
        validate_stage_records(case)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StageFailure("M6", "contract_drift", f"Invalid six-stage execution contract: {exc}") from exc
    report = case.get("dependency_report")
    if not isinstance(report, dict) or not isinstance(report.get("static"), dict):
        raise StageFailure("M6", "semantic_relation_unknown", "AST-pattern cases require a dependency report")
    static = report["static"]
    payload = {key: value for key, value in static.items() if key != "certificate_digest"}
    if static.get("certificate_digest") != digest(payload):
        raise StageFailure("M6", "contract_drift", "Dependency certificate was changed")
    if static.get("result_relation") != "proved":
        raise StageFailure("M6", "semantic_relation_unknown", "The full query relation has not been proved")
    queries = {side: case.get("query_ir", {}).get(side) for side in ("a", "b")}
    if (static.get("graph_digest") != digest(case.get("graph_spec")) or
            static.get("query_digest") != digest(queries) or
            static.get("bindings_digest") != digest(case.get("bindings", {}))):
        raise StageFailure("M6", "contract_drift", "Case inputs no longer match their dependency certificate")
    if any(check not in case.get("checks", ()) for check in case.get("bindings", {}).get("required_state_checks", ())):
        raise StageFailure("M6", "contract_drift", "A required rule premise check was dropped")
    # This AST version has graph/literal terminals, but no external Parameter
    # node. Compiler-owned per-side bindings are checked separately below.
    if case.get("parameters") != {}:
        raise StageFailure("M6", "contract_drift", "Unexpected external parameters for a fixed AST query")
    for field in ("query_ir", "query_bindings", "output_columns", "required_features"):
        if field in case and not isinstance(case[field], dict):
            raise StageFailure("M6", "contract_drift", f"Executable metadata {field} must be a mapping")
    if case.get("compile_error"):
        return  # The executor reports the preserved target capability failure.
    from ir import from_data
    from languages import generate
    target = (case.get("target_dbms", "neo4j") if database is None else database.get("dbms", "neo4j"))
    language = (case.get("language", "cypher") if database is None else database.get("language", "cypher"))
    features = (case.get("verified_features", ()) if database is None else database.get("verified_features", ()))
    try:
        compiled = {side: generate(from_data(value), target, language, verified_features=features)
                    for side, value in case["query_ir"].items()}
    except (ValueError, KeyError, TypeError) as exc:
        raise StageFailure("M6", "contract_drift", f"Executable query cannot be reproduced from its IR: {exc}") from exc
    for side, query in compiled.items():
        if (case.get(f"query_{side}") != query.text or
                digest(case.get("query_bindings", {}).get(side, {})) != digest(query.bindings) or
                case.get("output_columns", {}).get(side) != [list(item) for item in query.columns] or
                case.get("required_features", {}).get(side) != sorted(query.required_features)):
            raise StageFailure("M6", "contract_drift", f"Executable query {side} differs from its compiled IR")
    if set(case.get("query_bindings", {})) - set(compiled):
        raise StageFailure("M6", "contract_drift", "Bindings refer to an undeclared executable side")


def attach_runtime_evidence(case, report):
    """Return bound runtime evidence; never mutate saved AST/graph inputs."""
    if case.get("method_version") != METHOD_VERSION:
        return None
    static = case.get("dependency_report", {}).get("static", {})
    if not static:
        return None
    return runtime_evidence({"graph_digest": digest(case.get("graph_spec")),
                             "query_digest": digest({side: case.get("query_ir", {}).get(side) for side in ("a", "b")}),
                             "report": report},
                            graph_digest=static["graph_digest"], query_digest=static["query_digest"],
                            target_id=static["target_id"])
