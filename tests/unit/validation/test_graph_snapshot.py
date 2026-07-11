from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot, verify_graph_snapshot


class GraphStateAdapter:
    def __init__(self, *, wrong_property: bool = False) -> None:
        self.wrong_property = wrong_property

    def execute(self, query, parameters, query_kind, *, max_rows=None, timeout_seconds=None):
        rows = {
            "validation_graph_nodes": (
                {"node_id": 1, "labels": ["Person"], "properties": {"id": 1, "name": "Wrong" if self.wrong_property else "Ada"}},
                {"node_id": 2, "labels": ["Person"], "properties": {"id": 2}},
            ),
            "validation_graph_relationships": (
                {"start_id": 1, "end_id": 2, "relationship_type": "KNOWS", "properties": {"id": 7}},
            ),
        }[query_kind]
        return ExecutionResult(TargetSystem.NEO4J, query_kind, rows, 1.0, {"truncated": False})


class CaseFoldingGraphStateAdapter(GraphStateAdapter):
    def execute(self, query, parameters, query_kind, *, max_rows=None, timeout_seconds=None):
        result = super().execute(
            query,
            parameters,
            query_kind,
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
        )
        if query_kind == "validation_graph_nodes":
            rows = tuple({**row, "labels": [label.lower() for label in row["labels"]]} for row in result.rows)
        else:
            rows = tuple(
                {**row, "relationship_type": row["relationship_type"].lower()}
                for row in result.rows
            )
        return ExecutionResult(result.system, result.query_kind, rows, result.elapsed_ms, result.summary)

    def graph_identifier_normalizer(self, value: str) -> str:
        return value.lower()


def _snapshot() -> GraphSnapshot:
    return GraphSnapshot.from_statements(
        (
            "CREATE (n:Person {id: 1, name: 'Ada'})",
            "CREATE (n:Person {id: 2})",
            "MATCH (a {id: 1}), (b {id: 2}) MERGE (a)-[r:KNOWS {id: 7}]->(b)",
        )
    )


def test_graph_snapshot_verifier_reads_back_counts_labels_types_and_properties() -> None:
    verification = verify_graph_snapshot(GraphStateAdapter(), _snapshot())

    assert verification.verified
    assert verification.fingerprint == _snapshot().content_hash
    assert verification.reason_codes == ()


def test_graph_snapshot_verifier_rejects_wrong_content_even_when_shape_and_input_hash_match() -> None:
    verification = verify_graph_snapshot(GraphStateAdapter(wrong_property=True), _snapshot())

    assert not verification.verified
    assert verification.fingerprint is None
    assert verification.reason_codes == ("graph_fingerprint_mismatch",)


def test_graph_snapshot_verifier_honors_target_identifier_folding() -> None:
    verification = verify_graph_snapshot(CaseFoldingGraphStateAdapter(), _snapshot())

    assert verification.verified
    assert verification.fingerprint == _snapshot().content_hash
