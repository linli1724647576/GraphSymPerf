from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_builder_module():
    path = Path("scripts/build_agensgraph_gdsmith_compat.py")
    spec = importlib.util.spec_from_file_location("build_agensgraph_gdsmith_compat", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_agensgraph_compat_patch_adds_statement_rewriter() -> None:
    module = _load_builder_module()
    original = """package org.example.gdsmith.agensGraph;

import org.example.gdsmith.common.query.GDSmithResultSet;
import org.example.gdsmith.cypher.CypherConnection;

import java.sql.Connection;
import java.sql.Statement;
import java.util.Arrays;
import java.util.List;

public class AgensGraphConnection extends CypherConnection {

    private Connection connection;

    @Override
    public void executeStatement(String arg) throws Exception{
        Statement stmt = connection.createStatement();
        stmt.execute(arg);
    }

    @Override
    public List<GDSmithResultSet> executeStatementAndGet(String arg) throws Exception{
        Statement stmt = connection.createStatement();
        return Arrays.asList(new GDSmithResultSet(stmt.executeQuery(arg)));
    }
}
"""

    patched = module._patch_agens_connection_source(original)

    assert "agensCompatible(arg)" in patched
    assert "MULTI_LABEL_NODE" in patched
    assert "DOUBLE_QUOTED_STRING" in patched
    assert "singleQuotedStrings(singleLabelNodes(query))" in patched
import pytest

from graph_sym_perf.adapters.agensgraph import AgensGraphAdapter
from graph_sym_perf.adapters.apache_age import ApacheAgeAdapter


@pytest.mark.parametrize("adapter_type", (AgensGraphAdapter, ApacheAgeAdapter))
def test_graph_replay_rejects_multi_label_nodes_instead_of_dropping_a_label(adapter_type) -> None:
    adapter = object.__new__(adapter_type)

    with pytest.raises(ValueError, match="multi-label"):
        adapter.load_gdsmith_graph(("CREATE (n:First:Second {id: 1})",))
