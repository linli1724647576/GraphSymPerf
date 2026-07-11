from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_builder_module():
    path = Path("scripts/build_agensgraph_gdsmith_compat.py")
    spec = importlib.util.spec_from_file_location("build_agensgraph_gdsmith_compat", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_agensgraph_compat_patch_rejects_unsupported_syntax_without_rewriting() -> None:
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
    assert "GRAPHSYMPERF_REJECT_MULTI_LABEL" in patched
    assert "GRAPHSYMPERF_REJECT_DOUBLE_QUOTED_STRING" in patched
    assert 'replaceAll("$1")' not in patched
    assert "appendReplacement" not in patched
    assert "return query;" in patched


def test_compat_patch_fails_if_connection_execution_anchor_is_missing() -> None:
    module = _load_builder_module()
    source = "import java.sql.Statement;\npublic class ChangedConnection {}\n"

    with pytest.raises(RuntimeError, match="required compatibility guard"):
        module._patch_agens_connection_source(source)
