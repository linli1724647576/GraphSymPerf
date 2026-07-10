from graph_sym_perf.issues.registry import IssueRegistry, canonical_reduced_pair_hash
from graph_sym_perf.validation.models import DeveloperStatus, PipelineStage, VersionedIssueRecord


def _record(issue_id: str, query_a: str, query_b: str, **changes) -> VersionedIssueRecord:
    values = {
        "issue_id": issue_id,
        "stage": PipelineStage.SUBMIT_READY,
        "developer_status": DeveloperStatus.WAITING_REPLY,
        "dbms": "neo4j",
        "dbms_version": "2026.07",
        "pair_hash": canonical_reduced_pair_hash(query_a, query_b),
        "direction": "b",
        "rule_id": "pattern_order_symmetry",
        "detector_version": "d1",
        "validator_version": "v3",
        "reducer_version": "r2",
        "rule_version": "rules-v4",
        "discovered_at": "2026-07-10",
        "site_class": "match",
        "normalized_pattern": "two-node-pattern",
        "normalized_predicate": "property-range",
        "reduced_query_a": query_a,
        "reduced_query_b": query_b,
    }
    values.update(changes)
    return VersionedIssueRecord(**values)


def test_exact_pair_hash_normalizes_variables_but_preserves_constants_and_labels() -> None:
    first = (
        "MATCH (n:Person) WHERE n.age > 10 RETURN n",
        "MATCH (n:Person) WHERE true AND n.age > 10 RETURN n",
    )
    renamed = (
        "MATCH (person:Customer) WHERE person.age > 99 RETURN person",
        "MATCH (person:Customer) WHERE true AND person.age > 99 RETURN person",
    )

    variable_only = (
        "MATCH (person:Person) WHERE person.age > 10 RETURN person",
        "MATCH (person:Person) WHERE true AND person.age > 10 RETURN person",
    )

    assert canonical_reduced_pair_hash(*first) == canonical_reduced_pair_hash(*variable_only)
    assert canonical_reduced_pair_hash(*first) != canonical_reduced_pair_hash(*renamed)


def test_same_normalized_pair_is_one_report_within_a_dbms_but_not_across_dbms() -> None:
    first = _record("one", "MATCH (n:A) RETURN n", "MATCH (n:A) WHERE true RETURN n")
    renamed = _record("two", "MATCH (x:A) RETURN x", "MATCH (x:A) WHERE true RETURN x")
    other_dbms = _record(
        "three",
        "MATCH (x:B) RETURN x",
        "MATCH (x:B) WHERE true RETURN x",
        dbms="memgraph",
    )
    registry = IssueRegistry()
    registry.record(first)

    assert registry.exact_duplicate(renamed) == first
    assert registry.exact_duplicate(other_dbms) is None


def test_possible_duplicate_cluster_requires_manual_resolution_before_submission() -> None:
    first = _record("one", "MATCH (n:A) RETURN n", "MATCH (n:A) WHERE true RETURN n")
    similar = _record(
        "two",
        "MATCH (n:A)-[:R]->(m) RETURN n",
        "MATCH (n:A)-[:R]->(m) WHERE true RETURN n",
    )
    registry = IssueRegistry()
    registry.record(first)
    registry.record(similar)

    assert registry.possible_duplicate_ids("two") == ("one",)
    assert registry.possible_duplicate_ids("one") == ("two",)
    assert not registry.can_submit("one")
    assert not registry.can_submit("two")

    registry.resolve_possible_duplicate("two", distinct=True, evidence_url="https://example.test/review/2")

    assert registry.can_submit("two")


def test_bug_family_key_groups_cross_dbms_reports_without_collapsing_them() -> None:
    neo4j = _record("one", "MATCH (n:A) RETURN n", "MATCH (n:A) WHERE true RETURN n")
    memgraph = _record(
        "two",
        "MATCH (n:A) RETURN n",
        "MATCH (n:A) WHERE true RETURN n",
        dbms="memgraph",
    )

    assert IssueRegistry.bug_family_key(neo4j) == IssueRegistry.bug_family_key(memgraph)
