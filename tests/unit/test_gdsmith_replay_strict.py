from __future__ import annotations

import pytest

from graph_sym_perf.gdsmith.replay import (
    graph_manifest_from_replay,
    parse_gdsmith_replay_graph,
)
from graph_sym_perf.generation import GraphCorpus


def test_neutral_graph_lowering_rejects_unconsumed_statements() -> None:
    with pytest.raises(ValueError, match="UNSUPPORTED_GDSMITH_GRAPH_STATEMENT"):
        parse_gdsmith_replay_graph(("CREATE INDEX ON :A(id)",))


def test_replay_graph_manifest_binds_counts_and_sealed_fingerprint() -> None:
    statements = (
        "CREATE (n0:L0 {id: 0})",
        "CREATE (n1:L1 {id: 1})",
        "MATCH (a {id: 0}), (b {id: 1}) MERGE (a)-[r:T0 {id: 0}]->(b)",
    )

    manifest = graph_manifest_from_replay(statements, requested_max_nodes=128)

    assert manifest.requested_max_nodes == 128
    assert manifest.actual_nodes == 2
    assert manifest.actual_relationships == 1
    assert manifest.label_count == 2
    assert manifest.relationship_type_count == 1
    assert manifest.index_count == 0
    assert manifest.graph_fingerprint == GraphCorpus(statements).content_hash
