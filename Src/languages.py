"""Generate a small language subset from the shared query IR.

Gremlin supports a checked explicit-traversal subset on JanusGraph/HugeGraph/ArcadeDB.
GQL remains an extension point and fails explicitly until implemented.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

from ir import Alias, ConditionalQuery, QueryError, QueryTree, RowLimit, UnionAll, requirements
from query import projection_names, render_query


DBMS = ("neo4j", "memgraph", "falkordb", "arcadedb", "age", "ladybug", "janusgraph", "hugegraph")
LANGUAGES = ("cypher", "gremlin", "gql")
AGE_TESTED_LIMITS = frozenset({"relationship_alternatives", "label_rebinding", "all", "boolean_expression_coalesce"})

# JanusGraph 1.1.0 SystemTypeManager.ADDITIONAL_RESERVED_NAMES. These are
# ordinary logical property names in the IR, not JanusGraph's implicit keys.
JANUSGRAPH_RESERVED_PROPERTY_NAMES = frozenset({"key", "vertex", "edge", "element", "property", "label"})
JANUSGRAPH_PROPERTY_PREFIX = "_gsp_property_"


def gremlin_property_key(key: str, target: str) -> str:
    """Encode only provider-reserved property slots; never rewrite scalar values.

    Escape the prefix itself so a logical ``key`` and ``_gsp_property_key``
    remain different properties. Labels, IR, columns and GraphSpec stay logical.
    """
    if target == "janusgraph" and (key.lower() in JANUSGRAPH_RESERVED_PROPERTY_NAMES
                                  or key.startswith(JANUSGRAPH_PROPERTY_PREFIX)):
        return JANUSGRAPH_PROPERTY_PREFIX + key
    return key


def logical_gremlin_property_key(key: str, target: str) -> str:
    """Invert encoded native property names for independent graph diagnostics."""
    if target == "janusgraph" and key.startswith(JANUSGRAPH_PROPERTY_PREFIX):
        logical = key[len(JANUSGRAPH_PROPERTY_PREFIX):]
        if gremlin_property_key(logical, target) == key:
            return logical
    return key


class UnsupportedLanguage(QueryError):
    def __init__(self, language: str):
        self.language = language
        super().__init__(f"Language generator is not implemented: {language}")


class UnsupportedFeature(QueryError):
    def __init__(self, dbms: str, features: Iterable[str]):
        self.dbms = dbms
        self.features = frozenset(features)
        super().__init__(f"Unsupported query feature for {dbms}: {', '.join(sorted(self.features))}")


@dataclass(frozen=True)
class CompiledQuery:
    text: str
    # Physical engine labels and historical logical result labels, in order.
    columns: tuple[tuple[str, str], ...]
    required_features: frozenset[str]
    bindings: dict = field(default_factory=dict)

    @property
    def column_map(self) -> dict[str, str]:
        return dict(self.columns)

    def normalize_rows(self, rows: Iterable[Mapping]) -> list[dict]:
        """Restore logical column names without collapsing duplicate rows."""
        expected = {physical for physical, _ in self.columns}
        restored = []
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != expected:
                raise QueryError("Result columns do not match the compiled projection")
            restored.append({logical: row[physical] for physical, logical in self.columns})
        return restored


def validate_language(query: QueryTree, dbms: str = "neo4j", language: str = "cypher", *,
                      verified_features: Iterable[str] = ()) -> frozenset[str]:
    language = language.lower()
    recursive = requirements(query) & {"with_clause", "query_subquery", "recursive_row_stream", "row_limit", "call_scope"}
    if recursive and (dbms != "neo4j" or language != "cypher"):
        raise UnsupportedFeature(dbms, recursive)
    if language == "gremlin" and dbms in {"janusgraph", "hugegraph", "arcadedb"}:
        from gremlin import validate_gremlin
        return validate_gremlin(query, dbms, verified_features=verified_features)
    if language != "cypher":
        raise UnsupportedLanguage(language)
    if dbms not in DBMS:
        raise QueryError(f"Unknown database target: {dbms}")
    if dbms in {"janusgraph", "hugegraph"}:
        raise UnsupportedLanguage(language)
    needed = requirements(query)
    unsupported = set()
    if dbms != "neo4j" and "conditional_query" in needed:
        unsupported.add("conditional_query")
    if dbms in {"ladybug", "age", "arcadedb"} and "multi_label_conjunction" in needed:
        # Ladybug denotes label alternatives; AGE/ArcadeDB loaders do not
        # instantiate Neo4j-style label sets for one graph entity.
        unsupported.add("multi_label_conjunction")
    if dbms == "age":
        # Conservative tested fragment for AGE 1.7.0 / PostgreSQL 18.1, not a
        # claim about every release. Callers may explicitly enable a feature
        # after checking its syntax and semantics on their installed version.
        unsupported.update(needed & (AGE_TESTED_LIMITS - set(verified_features)))
    if dbms == "ladybug" and "mixed_type_coalesce" in needed:
        # Ladybug may coerce BOOL and INT64 operands to STRING; the shared IR
        # preserves the first non-NULL operand's scalar type.
        unsupported.add("mixed_type_coalesce")
    if dbms != "ladybug":
        unsupported.update(needed & {"path_mode_walk", "path_mode_acyclic"})
    if unsupported:
        raise UnsupportedFeature(dbms, unsupported)
    return needed


def _alias_outputs(query: QueryTree) -> QueryTree:
    if isinstance(query, RowLimit):
        return replace(query, body=_alias_outputs(query.body))
    if isinstance(query, UnionAll):
        return replace(query, branches=tuple(_alias_outputs(branch) for branch in query.branches))
    if isinstance(query, ConditionalQuery):
        return replace(query, then=_alias_outputs(query.then), otherwise=_alias_outputs(query.otherwise))
    return replace(query, returns=tuple(Alias(item.expression if isinstance(item, Alias) else item, f"c{index}")
                                        for index, item in enumerate(query.returns)))


def generate(query: QueryTree, dbms: str = "neo4j", language: str = "cypher", *,
             verified_features: Iterable[str] = ()) -> CompiledQuery:
    """Compile without normalizing away experimentally significant order.

    Target acceptance must still be probed for the exact installed version.
    This check rejects known semantic mismatches; it is not a claim that every
    identified feature is implemented by every release of the target engine.
    """
    verified_features = frozenset(verified_features)
    if language.lower() == "gremlin" and dbms in {"janusgraph", "hugegraph", "arcadedb"}:
        validate_language(query, dbms, language, verified_features=verified_features)
        from gremlin import generate_gremlin
        return generate_gremlin(query, dbms, verified_features=verified_features)
    needed = validate_language(query, dbms, language, verified_features=verified_features)
    return _render_compiled(query, dbms, needed)


def render_saved(query: QueryTree, dbms: str = "neo4j", language: str = "cypher") -> CompiledQuery:
    """Reconstruct archived text without applying today's version limits.

    This is solely an integrity/readback interface, not target support evidence.
    Execution must use generate or validate_language to apply current gates.
    IR scope/type validation and known language/dialect checks remain required.
    """
    if language.lower() == "gremlin" and dbms in {"janusgraph", "hugegraph", "arcadedb"}:
        from gremlin import generate_gremlin
        return generate_gremlin(query, dbms, verified_features=("gremlin_null_projection",))
    if language.lower() != "cypher":
        raise UnsupportedLanguage(language)
    if dbms not in DBMS:
        raise QueryError(f"Unknown database target: {dbms}")
    return _render_compiled(query, dbms, requirements(query))


def _render_compiled(query: QueryTree, dbms: str, needed: frozenset[str]) -> CompiledQuery:
    row_stream_features = needed & {"recursive_row_stream", "row_limit", "call_scope"}
    if dbms != "neo4j" and row_stream_features:
        raise UnsupportedFeature(dbms, row_stream_features)
    logical = projection_names(query)
    if dbms in ("neo4j", "memgraph", "falkordb"):
        return CompiledQuery(render_query(query), tuple(zip(logical, logical)), needed)
    compiled = _alias_outputs(query)
    dialect = "ladybug" if dbms == "ladybug" else "cypher"
    physical = tuple(f"c{index}" for index in range(len(logical)))
    return CompiledQuery(render_query(compiled, dialect), tuple(zip(physical, logical)), needed)
