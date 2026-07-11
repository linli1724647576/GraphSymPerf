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


def test_agensgraph_compat_patch_renders_only_safe_gdsmith_literals_and_rejects_the_rest() -> None:
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
    assert "SAFE_DOUBLE_QUOTED_LITERAL" in patched
    assert "GRAPHSYMPERF_REJECT_MULTI_LABEL" in patched
    assert "GRAPHSYMPERF_REJECT_UNSAFE_DOUBLE_QUOTED_TOKEN" in patched
    assert ".replaceAll(\"'$1'\")" in patched
    assert patched.count("stmt.setQueryTimeout(60);") == 2
    assert "appendReplacement" not in patched
    assert "return rendered;" in patched


def test_arcadedb_compat_patch_sets_a_per_statement_jdbc_timeout() -> None:
    module = _load_builder_module()
    original = """package org.example.gdsmith.arcadeDB;

import org.example.gdsmith.common.query.GDSmithResultSet;
import org.example.gdsmith.cypher.CypherConnection;

import java.sql.Connection;
import java.sql.Statement;
import java.util.Arrays;
import java.util.List;

public class ArcadeDBConnection extends CypherConnection {
    public Connection connection;

    @Override
    public void executeStatement(String arg) throws Exception{
        Statement stmt = connection.createStatement();
        stmt.execute("{cypher}" + arg);
    }

    @Override
    public List<GDSmithResultSet> executeStatementAndGet(String arg) throws Exception{
        Statement stmt = connection.createStatement();
        return Arrays.asList(new GDSmithResultSet(stmt.executeQuery("{cypher}" + arg)));
    }
}
"""

    patched = module._patch_arcadedb_connection_source(original)

    assert patched.count("stmt.setQueryTimeout(60);") == 2


def test_compat_patch_fails_if_connection_execution_anchor_is_missing() -> None:
    module = _load_builder_module()
    source = "import java.sql.Statement;\npublic class ChangedConnection {}\n"

    with pytest.raises(RuntimeError, match="required compatibility guard"):
        module._patch_agens_connection_source(source)


def test_build_patches_remove_unbuildable_provider_and_add_runnable_shade_config(tmp_path) -> None:
    module = _load_builder_module()
    main = tmp_path / module.MAIN_RELATIVE_PATH
    main.parent.mkdir(parents=True)
    main.write_text(
        "import org.example.gdsmith.tugraph.TuGraphProvider;\n"
        "            providers.add(new TuGraphProvider());\n",
        encoding="utf-8",
    )
    pom = tmp_path / "pom.xml"
    pom.write_text("<project>\n</project>\n", encoding="utf-8")

    main_patch = module._patch_main_provider_registry(tmp_path)
    pom_patch = module._patch_maven_build(tmp_path)

    assert "TuGraphProvider" not in main.read_text(encoding="utf-8")
    patched_pom = pom.read_text(encoding="utf-8")
    assert "org/example/gdsmith/tugraph/**" in patched_pom
    assert "maven-shade-plugin" in patched_pom
    assert "org.example.gdsmith.Main" in patched_pom
    assert main_patch.is_file()
    assert pom_patch.is_file()
