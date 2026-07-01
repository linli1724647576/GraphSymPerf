from __future__ import annotations

from graph_sym_perf.adapters.falkordb import FalkorDBAdapter
from graph_sym_perf.adapters.memgraph import MemgraphAdapter


def test_memgraph_filter_rejects_string_prefix_suffix_operators() -> None:
    adapter = object.__new__(MemgraphAdapter)

    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE n.k1 STARTS WITH n.k1 RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE n.k1 ENDS WITH n.k1 RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE (n.k1) RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE ((n.k1) AND true) RETURN n")
    assert adapter.gdsmith_query_is_replayable("MATCH (n) WHERE n.k1 = n.k1 RETURN n")


def test_falkordb_filter_rejects_string_ops_and_bare_property_predicates() -> None:
    adapter = object.__new__(FalkorDBAdapter)

    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE n.k1 STARTS WITH n.k1 RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE (r0.k55) RETURN n")
    assert not adapter.gdsmith_query_is_replayable("MATCH (n) WHERE ((r0.k55) OR true) RETURN n")
    assert adapter.gdsmith_query_is_replayable("MATCH (n) WHERE r0.k55 = 1 RETURN n")
