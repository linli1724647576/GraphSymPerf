"""Import real GDsmith seeds and instantiate cost-aware, read-only cases.

Derivation of a restricted seed may change the original query's result. Only
the later mutation of the populated query is required to preserve semantics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import random
import re
import subprocess
import time
from typing import Any

from state import GraphSpec

from query import (
    Alias,
    All,
    Boolean,
    Call,
    Case,
    Comparison,
    Concat,
    ConditionalQuery,
    Count,
    Edge,
    Exists,
    ListExpr,
    Literal,
    Match,
    Negation,
    Node,
    Property,
    Query,
    Relationship,
    UnionAll,
    Variable,
    clauses,
    extract_seed_pattern,
    extract_seed_boolean,
    render_expression,
    render_query,
    validate_query,
    with_clauses,
)


class PatternNotApplicable(ValueError):
    """The input seed has no structural site for the requested pattern."""


class SeedImportError(ValueError):
    """A seed log or its provenance cannot be imported faithfully."""


@dataclass(frozen=True)
class SeedQuery:
    text: str
    source: dict[str, Any]


@dataclass(frozen=True)
class Placeholder:
    name: str
    kind: str
    role: str
    variables: tuple[str, ...]


@dataclass(frozen=True)
class Skeleton:
    pattern: str
    seed_pattern: Node | Edge
    operator: str | None
    placeholders: tuple[Placeholder, ...]
    text: str
    source: dict[str, Any]


_WRITE_WORDS = {
    "CREATE",
    "MERGE",
    "DELETE",
    "DETACH",
    "SET",
    "REMOVE",
    "DROP",
    "LOAD",
    "FOREACH",
    "CALL",
}
_SOURCE_FIELDS = {
    "generator",
    "source_log",
    "source_log_sha256",
    "source_log_seed",
    "gdsmith_commit",
    "gdsmith_dirty",
    "jar_sha256",
    "jar_provenance_note",
    "fixture_sha256",
    "source_repository",
}


def _masked(text: str) -> str:
    """Mask quoted tokens/comments without moving source offsets."""
    return re.sub(
        r"'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|`(?:``|[^`])*`|/\*[\s\S]*?\*/|//[^\n]*",
        lambda match: " " * len(match.group()),
        text,
    )


def _words(text: str) -> list[re.Match[str]]:
    return list(re.finditer(r"(?<![\w.:$])[A-Za-z_][A-Za-z_0-9]*\b", _masked(text)))


def import_log(path: str | Path, *, limit: int | None = None) -> list[SeedQuery]:
    """Import complete, one-line read statements from a GDsmith log.

    The original statement and its line/hash are retained. Setup statements
    are intentionally not reused: population constructs a separate graph.
    """
    path = Path(path)
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("limit must be a positive integer")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    digest = sha256(raw).hexdigest()
    metadata: dict[str, Any] = {}
    sidecar = path.with_suffix(".source.json")
    if sidecar.exists():
        loaded = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise SeedImportError("seed provenance must be an object")
        if loaded.get("fixture_sha256") != digest:
            raise SeedImportError("seed fixture does not match its recorded SHA-256")
        metadata = loaded
    seed_match = re.search(r"(?m)^--\s*seed value:\s*(-?\d+)\s*$", text)
    source: dict[str, Any] = {
        "generator": "gdsmith",
        "source_log": str(path),
        "source_log_sha256": digest,
        "source_log_seed": int(seed_match.group(1)) if seed_match else None,
        "gdsmith_commit": None,
        "jar_sha256": None,
    }
    source.update({key: value for key, value in metadata.items() if key in _SOURCE_FIELDS})
    source.update({"import_log": str(path), "import_log_sha256": digest})
    excerpts = {item["fixture_line"]: item for item in metadata.get("excerpts", [])}
    seeds: list[SeedQuery] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        statement = line.strip()
        if not statement.endswith(";"):
            continue
        statement = statement[:-1].strip()
        words = [match.group().upper() for match in _words(statement)]
        if (
            not words
            or words[0] != "MATCH"
            or "RETURN" not in words
            or _WRITE_WORDS.intersection(words)
        ):
            continue
        query_hash = sha256(statement.encode()).hexdigest()
        if query_hash in seen:
            continue
        provenance = {**source, "import_line": line_number, "query_sha256": query_hash}
        if line_number in excerpts:
            excerpt = excerpts[line_number]
            if excerpt.get("query_sha256") != query_hash:
                raise SeedImportError("query excerpt does not match its recorded SHA-256")
            provenance["source_line"] = excerpt["original_line"]
        else:
            provenance["source_line"] = line_number
        seeds.append(SeedQuery(statement, provenance))
        seen.add(query_hash)
        if limit is not None and len(seeds) >= limit:
            break
    return seeds


def load_seeds(config: dict[str, Any]) -> list[SeedQuery]:
    """Optionally run an explicit argv, then import explicitly named logs."""
    files = config.get("log_files")
    if not isinstance(files, list) or not files or not all(isinstance(item, str) for item in files):
        raise ValueError("generation.log_files must be a nonempty list of paths")
    limit = config.get("limit", 20)
    if type(limit) is not int or limit <= 0:
        raise ValueError("generation.limit must be a positive integer")
    command = config.get("command")
    run_provenance: dict[str, Any] = {}
    if command is not None:
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg for arg in command)
        ):
            raise ValueError("generation.command must be a nonempty argv list")
        timeout = config.get("timeout_seconds", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("generation.timeout_seconds must be positive")
        previous = {
            file: Path(file).stat().st_mtime_ns if Path(file).exists() else None for file in files
        }
        started = time.time_ns()
        try:
            completed = subprocess.run(
                command,
                cwd=config.get("cwd"),
                timeout=timeout,
                check=False,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise SeedImportError(
                "external seed generation timed out; argv and output are not recorded"
            ) from None
        if completed.returncode != 0:
            raise SeedImportError(
                f"external seed generation exited with code {completed.returncode}"
            )
        for file in files:
            path = Path(file)
            if (
                not path.is_file()
                or path.stat().st_mtime_ns < started
                or path.stat().st_mtime_ns == previous[file]
            ):
                raise SeedImportError("external generator did not refresh all configured logs")
        if "-jar" in command:
            position = command.index("-jar") + 1
            if position < len(command):
                jar = Path(command[position])
                if not jar.is_absolute():
                    jar = Path(config.get("cwd") or ".") / jar
                if jar.is_file():
                    run_provenance["jar_sha256"] = sha256(jar.read_bytes()).hexdigest()
                    manifest = jar.with_suffix(jar.suffix + ".manifest.json")
                    if manifest.exists():
                        body = json.loads(manifest.read_text(encoding="utf-8"))
                        if body.get("sha256") != run_provenance["jar_sha256"]:
                            raise SeedImportError(
                                "external generator JAR differs from its build manifest"
                            )
                        run_provenance["gdsmith_commit"] = body.get("source_commit")
    seeds: list[SeedQuery] = []
    seen: set[str] = set()
    for file in files:
        for seed in import_log(file):
            if seed.source["query_sha256"] in seen:
                continue
            seeds.append(SeedQuery(seed.text, {**seed.source, **run_provenance}))
            seen.add(seed.source["query_sha256"])
            if len(seeds) >= limit:
                return seeds
    if not seeds:
        raise SeedImportError("no complete GDsmith read queries found in the configured logs")
    return seeds


_ENRICHMENTS = {
    "P2": (
        "derive_inverse_relationship_pair",
        "MATCH $lowCostNode(u)-[:$forwardType]->$highCostNode(v) RETURN u.uid, v.uid",
    ),
    "P3": (
        "derive_bounded_path_from_edge",
        "MATCH p = $lowCostNode(u)-[:$R*$min..$max]-$highCostNode(v) RETURN u.uid, v.uid, length(p) AS hops",
    ),
    "P6": (
        "derive_property_equality_filter",
        "MATCH $highCostPattern(u,v) WHERE u.$indexedKey = $lowCostValue RETURN u.uid, v.uid",
    ),
    "P7": (
        "derive_multi_label_conjunction",
        "MATCH (u:$lowCostLabel:$highCostLabel)-[:$R]->(v) RETURN u.uid, v.uid",
    ),
    "P8": (
        "derive_relationship_type_disjunction",
        "MATCH $lowCostNode(u)-[:$lowCostType|$highCostType]->(v) WHERE v.keep = true RETURN u.uid, v.uid",
    ),
    "P9": (
        "derive_two_match_predicate_site",
        "MATCH (u:$U) WHERE $lowCostPredicate(u) MATCH $highCostPattern(u,v) RETURN u.uid, v.uid",
    ),
    "P10": (
        "derive_independent_match_branches",
        "MATCH $lowCostPattern(a) MATCH $highCostPattern(b,c) RETURN a.uid, b.uid, c.uid",
    ),
    "P11": (
        "derive_shared_anchor_branches",
        "MATCH $lowCostNode(u) MATCH $lowCostPattern(u,v) MATCH $highCostPattern(u,w) RETURN u.uid, v.uid, w.uid",
    ),
    "P12": (
        "derive_two_match_chain",
        "MATCH $lowCostNode(u)-[:$R]->(v) MATCH (v)-[:$S]->$highCostNode(w) RETURN u.uid, w.uid",
    ),
    "P13": (
        "derive_independent_optional_branches",
        "MATCH $lowCostNode(u) OPTIONAL MATCH $lowCostPattern(u,v) OPTIONAL MATCH $highCostPattern(u,w) RETURN u.uid, v.uid, w.uid",
    ),
    "P14": ("derive_union_all_from_seed_projection", "$lowCostQuery UNION ALL $highCostQuery"),
    "P15": (
        "derive_case_expression_from_seed",
        "MATCH (u:$U) RETURN u.uid, CASE WHEN $lowCostBool THEN $lowCostExpr ELSE $highCostExpr END AS value",
    ),
    "P16": (
        "derive_list_quantifier_from_seed",
        "RETURN all(x IN ($lowCostList + $highCostList) WHERE $predicate(x)) AS ok",
    ),
}


# Typed cost roles are execution metadata; the display text above is not.
# Keep their order and names stable for previously recorded cases.
_ENRICHMENT_ROLES = {
    "P2": (("lowCostNode", "node_pattern", "low", ("u",)),
           ("forwardType", "relationship_type", "structural", ()),
           ("highCostNode", "node_pattern", "high", ("v",))),
    "P3": (("lowCostNode", "node_pattern", "low", ("u",)),
           ("R", "structural", "structural", ()),
           ("min", "integer", "structural", ()), ("max", "integer", "structural", ()),
           ("highCostNode", "node_pattern", "high", ("v",))),
    "P6": (("highCostPattern", "graph_pattern", "high", ("u", "v")),
           ("indexedKey", "property_key", "structural", ()),
           ("lowCostValue", "scalar_expression", "low", ())),
    "P7": (("lowCostLabel", "label", "low", ()), ("highCostLabel", "label", "high", ()),
           ("R", "structural", "structural", ())),
    "P8": (("lowCostNode", "node_pattern", "low", ("u",)),
           ("lowCostType", "relationship_type", "low", ()),
           ("highCostType", "relationship_type", "high", ())),
    "P9": (("U", "structural", "structural", ()),
           ("lowCostPredicate", "boolean_expression", "low", ("u",)),
           ("highCostPattern", "graph_pattern", "high", ("u", "v"))),
    "P10": (("lowCostPattern", "graph_pattern", "low", ("a",)),
            ("highCostPattern", "graph_pattern", "high", ("b", "c"))),
    "P11": (("lowCostNode", "node_pattern", "low", ("u",)),
            ("lowCostPattern", "graph_pattern", "low", ("u", "v")),
            ("highCostPattern", "graph_pattern", "high", ("u", "w"))),
    "P12": (("lowCostNode", "node_pattern", "low", ("u",)),
            ("R", "structural", "structural", ()), ("S", "structural", "structural", ()),
            ("highCostNode", "node_pattern", "high", ("w",))),
    "P13": (("lowCostNode", "node_pattern", "low", ("u",)),
            ("lowCostPattern", "graph_pattern", "low", ("u", "v")),
            ("highCostPattern", "graph_pattern", "high", ("u", "w"))),
    "P14": (("lowCostQuery", "query", "low", ()), ("highCostQuery", "query", "high", ())),
    "P15": (("U", "structural", "structural", ("u",)),
            ("lowCostBool", "boolean_expression", "low", ("u",)),
            ("lowCostExpr", "scalar_expression", "low", ("u",)),
            ("highCostExpr", "scalar_expression", "high", ("u",))),
    "P16": (("lowCostList", "list_expression", "low", ()),
            ("highCostList", "list_expression", "high", ()),
            ("predicate", "boolean_expression", "structural", ("x",))),
}


_SCOR_SPECS = {
    "S1": (
        "1.1",
        "RETURN $lowCostBool OR $highCostBool(u) AS value",
        "OR with a decisive true operand",
    ),
    "S2": (
        "1.2",
        "RETURN $lowCostBool AND $highCostBool(u) AS value",
        "AND with a decisive false operand",
    ),
    "S3": (
        "2.1",
        "WHEN $lowCostBool THEN $lowCostQuery ELSE $highCostQuery",
        "SQL IF adapted to native Cypher 25 conditional queries",
    ),
    "S4": (
        "2.2",
        "RETURN CASE WHEN $lowCostBool THEN $lowCostExpr ELSE $highCostExpr(u) END AS value",
        "CASE with a high-cost expression in the unselected branch",
    ),
    "S5": (
        "3.1",
        "MATCH $highCostPattern(u,v) RETURN 1 AS value LIMIT $lowCostLimit",
        "LIMIT 0 or a false filter with no result-resurrecting aggregate",
    ),
    "S6": (
        "3.2",
        "MATCH $highCostPattern(u,v) RETURN 1 AS value LIMIT $lowCostLimit",
        "Both regions provide at least n rows with identical constant projections",
    ),
    "S7": (
        "4.1",
        "RETURN coalesce(NULL, $lowCostExpr, $highCostExpr(u)) AS value",
        "A non-null prefix makes the high-cost suffix irrelevant",
    ),
    "S8": (
        "4.2",
        "MATCH $lowCostNode(u) WHERE EXISTS { MATCH $highCostPattern(u,v) WHERE v.key = u.key } RETURN u.uid",
        "Graph semi-join with every candidate matching in both regions",
    ),
    "S9": (
        "5.1",
        "MATCH (e:ScorRoot) WHERE $lowCostBool MATCH $highCostPattern(u,v) RETURN 1 AS value",
        "An empty upstream MATCH suppresses an independent graph join",
    ),
    "S10": (
        "5.2",
        "MATCH (u:ScorRoot {id: 1}) WHERE $lowCostBool AND $membershipBool(u) RETURN DISTINCT u.key AS key",
        "SQL INTERSECT/EXCEPT adapted through DISTINCT and null-safe EXISTS/NOT EXISTS; not native Cypher set operators",
    ),
}


def _scor_placeholders(pattern: str) -> tuple[Placeholder, ...]:
    """SCor role types are contracts, independent of their rendered spelling."""
    control = {
        "S1": ("lowCostBool", "boolean_expression", ()),
        "S2": ("lowCostBool", "boolean_expression", ()),
        "S3": ("lowCostBool", "boolean_expression", ()),
        "S4": ("lowCostBool", "boolean_expression", ()),
        "S5": ("lowCostLimit", "integer", ()),
        "S6": ("lowCostLimit", "integer", ()),
        "S7": ("lowCostExpr", "scalar_expression", ()),
        "S8": ("lowCostNode", "node_pattern", ("u",)),
        "S9": ("lowCostBool", "boolean_expression", ()),
        "S10": ("lowCostBool", "boolean_expression", ()),
    }[pattern]
    expensive = {
        "S1": ("highCostBool", "boolean_expression", ("u",)),
        "S2": ("highCostBool", "boolean_expression", ("u",)),
        "S3": ("highCostQuery", "query", ()),
        "S4": ("highCostExpr", "scalar_expression", ("u",)),
        "S5": ("highCostPattern", "graph_pattern", ("u", "v")),
        "S6": ("highCostPattern", "graph_pattern", ("u", "v")),
        "S7": ("highCostExpr", "scalar_expression", ("u",)),
        "S8": ("highCostPattern", "graph_pattern", ("u", "v")),
        "S9": ("highCostPattern", "graph_pattern", ("u", "v")),
        "S10": ("membershipBool", "boolean_expression", ("u",)),
    }[pattern]
    slots = [
        Placeholder(control[0], control[1], "low", control[2]),
        Placeholder(expensive[0], expensive[1], "high", expensive[2]),
        Placeholder("referencePattern", "graph_pattern", "low", ("u", "v")),
    ]
    if pattern == "S3":
        slots.append(Placeholder("lowCostQuery", "query", "low", ()))
    elif pattern == "S4":
        slots.append(Placeholder("lowCostExpr", "scalar_expression", "low", ()))
    return tuple(slots)


def abstract_seed(seed_query: SeedQuery | str | None, pattern: str) -> Skeleton:
    """Extract a real seed structure and mark typed cost roles.

    Full seed semantics are not preserved: the derivation is explicitly
    recorded, and equality is only required after population and mutation.
    """
    if pattern not in {"P1", "P4", "P5", *_ENRICHMENTS, *_SCOR_SPECS}:
        raise PatternNotApplicable(f"pattern {pattern!r} is not implemented")
    builtin = seed_query is None
    if builtin:
        seed_pattern = Edge(Node("u"), Relationship(type="R"), Node("v"))
        source = {"generator": "builtin_template", "builtin_pattern": pattern,
                  "raw_query": None, "derivation_preserves_original_result": False}
    else:
        if isinstance(seed_query, str):
            seed_query = SeedQuery(
                seed_query,
                {"generator": "provided", "query_sha256": sha256(seed_query.encode()).hexdigest()},
            )
        if not isinstance(seed_query, SeedQuery):
            raise TypeError("seed_query must be SeedQuery or str")
        try:
            seed_pattern = extract_seed_pattern(seed_query.text)
        except ValueError as exc:
            raise PatternNotApplicable(f"seed has no supported first MATCH fragment: {exc}") from exc
        source = {**seed_query.source, "raw_query": seed_query.text,
                  "derivation_preserves_original_result": False}
    changes: list[str] = [
        "derive_first_match_fragment",
        "rebind_labels_types_properties_and_projection",
    ]
    if pattern == "P1":
        if not isinstance(seed_pattern, Edge):
            raise PatternNotApplicable("P1 requires a single-edge fragment in the seed")
        if seed_pattern.left.variable == seed_pattern.right.variable:
            raise PatternNotApplicable("P1 requires distinct endpoint variables")
        left, right = seed_pattern.left.variable, seed_pattern.right.variable
        changes.append("generalize_seed_edge_to_undirected")
        placeholders = (
            Placeholder("lowCostNode", "node_pattern", "low", (left,)),
            Placeholder("R", "relationship_type", "structural", ()),
            Placeholder("highCostNode", "node_pattern", "high", (right,)),
        )
        text = f"MATCH $lowCostNode({left})-[:$R]-$highCostNode({right}) RETURN {left}.uid, {right}.uid"
        operator = None
    elif pattern in {"P4", "P5"}:
        operator = "AND" if pattern == "P4" else "OR"
        if not builtin:
            site = extract_seed_boolean(seed_query.text, operator)
            if site is None:
                raise PatternNotApplicable(
                    f"{pattern} requires an actual {operator} in a seed WHERE clause"
                )
            source["boolean_expression_span"] = list(site)
        primary = seed_pattern.left if isinstance(seed_pattern, Edge) else seed_pattern
        variable = primary.variable
        changes.extend(
            ["derive_primary_node_match", "retain_boolean_operator_and_repopulate_operands"]
        )
        placeholders = (
            Placeholder("lowCostPredicate", "boolean_expression", "low", (variable,)),
            Placeholder("highCostBool", "boolean_expression", "high", (variable,)),
        )
        text = f"MATCH ({variable}:$U) WHERE $lowCostPredicate({variable}) {operator} $highCostBool({variable}) RETURN {variable}.uid"
    elif pattern in _SCOR_SPECS:
        paper_pattern, text, adaptation = _SCOR_SPECS[pattern]
        changes.extend(
            ["derive_scor_graph_short_circuit_context", "rename_derived_variables_to_pattern_roles"]
        )
        source.update(
            {
                "seed_syntax_enriched": True,
                "enrichment_origin": "python_seed_adapter",
                "paper_doi": "10.1145/3802061",
                "paper_table": "Table 1",
                "paper_pattern": paper_pattern,
                "graph_adaptation": adaptation,
                "oracle_mode": "high_cost_reference_replacement",
            }
        )
        operator = "OR" if pattern == "S1" else "AND" if pattern == "S2" else None
        placeholders = _scor_placeholders(pattern)
    else:
        operation, text = _ENRICHMENTS[pattern]
        changes.extend([operation, "rename_derived_variables_to_pattern_roles"])
        source["seed_syntax_enriched"] = True
        source["enrichment_origin"] = "python_seed_adapter"
        operator = None
        placeholders = tuple(Placeholder(*role) for role in _ENRICHMENT_ROLES[pattern])
    source["derivation_steps"] = changes
    source["generation_mode"] = "builtin_template" if builtin else "seed_adapter"
    if builtin:
        source.update({
            "raw_query": None,
            "seed_syntax_enriched": False,
            "enrichment_origin": "builtin_template",
            "derivation_steps": ["instantiate_builtin_pattern", "populate_cost_roles"],
        })
    return Skeleton(pattern, seed_pattern, operator, placeholders, text, source)


def _index_check(label: str, property_name: str) -> dict[str, Any]:
    return {
        "query": (
            "SHOW INDEXES YIELD name, state, labelsOrTypes, properties "
            "WHERE name = 'gsp_cost_lookup' "
            "RETURN name, state, labelsOrTypes, properties"
        ),
        "expected_rows": [
            {
                "name": "gsp_cost_lookup",
                "state": "ONLINE",
                "labelsOrTypes": [label],
                "properties": [property_name],
            }
        ],
    }


def _count_check(query: str, count: int) -> dict[str, Any]:
    return {"query": query, "expected_rows": [{"count": count}]}


def _build_initial(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    """The original endpoint and boolean cases share their small graph family."""
    pattern = skeleton.pattern
    offset = random.Random(seed).randrange(10_000, 1_000_000)
    fanout = 4
    if pattern == "P1":
        original = skeleton.seed_pattern
        assert isinstance(original, Edge)
        left, right = original.left.variable, original.right.variable
        graph_pattern = Edge(
            Node(left, ("GspLow",), (("id", Literal(1)),)),
            Relationship(type="GSP_LINK"),
            Node(right, ("GspHigh",)),
            "undirected",
        )
        query = Query(graph_pattern, None, (Property(left, "uid"), Property(right, "uid")))
        setup = [
            "CREATE INDEX gsp_cost_lookup IF NOT EXISTS FOR (n:GspLow) ON (n.id)",
            "CREATE (u:GspLow {id: 1, uid: 1})-[:GSP_LINK]->(v:GspHigh {uid: 2})",
            (
                f"UNWIND range(1, {scale}) AS i "
                f"CREATE (v:GspHigh {{uid: {offset} + {fanout + 1} * i}}) "
                f"WITH v, i UNWIND range(1, {fanout}) AS j "
                f"CREATE (u:GspLow {{id: {offset} + {fanout + 1} * i + j, "
                f"uid: {offset} + {fanout + 1} * i + j}})-[:GSP_LINK]->(v)"
            ),
        ]
        checks = [
            _index_check("GspLow", "id"),
            _count_check("MATCH (u:GspLow {id: 1}) RETURN count(u) AS count", 1),
            _count_check("MATCH (v:GspHigh) RETURN count(v) AS count", scale + 1),
            _count_check(
                "MATCH (:GspLow)-[r:GSP_LINK]->(:GspHigh) RETURN count(r) AS count",
                fanout * scale + 1,
            ),
        ]
        expected = [{f"{left}.uid": 1, f"{right}.uid": 2}]
        bindings = {
            "lowCostNode": {"label": "GspLow", "key": "id", "value": 1, "candidates": 1},
            "highCostNode": {"label": "GspHigh", "candidates": scale + 1},
            "R": "GSP_LINK",
            "noise_relationships": fanout * scale,
        }
    else:
        primary = (
            skeleton.seed_pattern.left
            if isinstance(skeleton.seed_pattern, Edge)
            else skeleton.seed_pattern
        )
        variable = primary.variable
        leaf = "gsp_leaf" if variable != "gsp_leaf" else "gsp_leaf_1"
        low = Comparison(Property(variable, "keep"), "=", Literal(True))
        high = Exists(
            Edge(Node(variable), Relationship(type="GSP_EXPAND"), Node(leaf, ("GspLeaf",)), "out"),
            Comparison(Property(leaf, "mark"), "=", Literal(True)),
        )
        query = Query(
            Node(variable, ("GspAnchor",)),
            Boolean(skeleton.operator, (low, high)),
            (Property(variable, "uid"),),
        )
        noise_keep, witness_keep = (False, True) if pattern == "P4" else (True, False)
        setup = [
            "CREATE INDEX gsp_cost_lookup IF NOT EXISTS FOR (n:GspAnchor) ON (n.uid)",
            f"CREATE (:GspAnchor {{uid: 1, keep: {str(noise_keep).lower()}}})",
            f"CREATE (:GspAnchor {{uid: 2, keep: {str(witness_keep).lower()}}})-[:GSP_EXPAND]->(:GspLeaf {{uid: 3, mark: true}})",
            f"MATCH (u:GspAnchor {{uid: 1}}) UNWIND range(1, {fanout * scale}) AS i CREATE (u)-[:GSP_EXPAND]->(:GspLeaf {{uid: {offset} + i, mark: false}})",
        ]
        checks = [
            _index_check("GspAnchor", "uid"),
            _count_check("MATCH (u:GspAnchor) RETURN count(u) AS count", 2),
            {
                "query": "MATCH (u:GspAnchor {uid: 1}) RETURN u.keep AS low_value",
                "expected_rows": [{"low_value": noise_keep}],
            },
            _count_check(
                "MATCH (:GspAnchor {uid: 1})-[:GSP_EXPAND]->(v:GspLeaf) RETURN count(v) AS count",
                fanout * scale,
            ),
            _count_check(
                "MATCH (:GspAnchor {uid: 1})-[:GSP_EXPAND]->(v:GspLeaf) WHERE v.mark = true RETURN count(v) AS count",
                0,
            ),
            _count_check(
                "MATCH (:GspAnchor {uid: 2})-[:GSP_EXPAND]->(v:GspLeaf) WHERE v.mark = true RETURN count(v) AS count",
                1,
            ),
        ]
        expected = [{f"{variable}.uid": uid} for uid in ([2] if pattern == "P4" else [1, 2])]
        bindings = {
            "lowCostPredicate": {
                "key": "keep",
                "comparison_value": True,
                "noise_value": noise_keep,
            },
            "highCostBool": {
                "kind": "exists",
                "relationship_type": "GSP_EXPAND",
                "leaf_label": "GspLeaf",
                "predicate": "mark = true",
                "noise_candidates": fanout * scale,
                "noise_matches": 0,
                "witness_matches": 1,
            },
            "anchors": 2,
        }
    validate_query(query)
    return {
        "pattern": pattern,
        "scale": scale,
        "seed": seed,
        "setup": setup,
        "query_a": query,
        "parameters": {},
        "checks": checks,
        "expected_rows": expected,
        "bindings": bindings,
        "skeleton": skeleton.text,
        "placeholders": [
            {**asdict(p), "variables": list(p.variables)} for p in skeleton.placeholders
        ],
        "source": {
            **skeleton.source,
            "populated_query_sha256": sha256(render_query(query).encode()).hexdigest(),
        },
    }


def _pack(
    skeleton: Skeleton,
    scale: int,
    seed: int,
    query: Query | UnionAll | ConditionalQuery,
    setup: list[str],
    checks: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    bindings: dict[str, Any],
) -> dict[str, Any]:
    validate_query(query)
    return {
        "pattern": skeleton.pattern,
        "scale": scale,
        "seed": seed,
        "setup": setup,
        "query_a": query,
        "parameters": {},
        "checks": checks,
        "expected_rows": rows,
        "bindings": bindings,
        "skeleton": skeleton.text,
        "placeholders": [
            {**asdict(p), "variables": list(p.variables)} for p in skeleton.placeholders
        ],
        "source": {
            **skeleton.source,
            "populated_query_sha256": sha256(render_query(query).encode()).hexdigest(),
        },
    }


def _index(label: str, key: str = "uid") -> str:
    return f"CREATE INDEX gsp_cost_lookup IF NOT EXISTS FOR (n:{label}) ON (n.{key})"


def _node(variable: str, label: str, key: str | None = None, value: int = 1) -> Node:
    return Node(variable, (label,), ((key, Literal(value)),) if key else ())


def _edge(left: Node, type_name: str, right: Node) -> Edge:
    return Edge(left, Relationship(type=type_name), right)


def _eq(variable: str, key: str, value: Any) -> Comparison:
    return Comparison(Property(variable, key), "=", Literal(value))


def _returns(*variables: str) -> tuple[Property, ...]:
    return tuple(Property(variable, "uid") for variable in variables)


def _offset(seed: int) -> int:
    return random.Random(seed).randrange(10_000, 1_000_000)


def _build_p2(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    offset = _offset(seed)
    setup = [
        _index("GspLow", "id"),
        (
            "CREATE (u:GspLow {uid: 1, id: 1})-[f:GSP_FORWARD {pair: 1}]->"
            "(v:GspHigh {uid: 2}) CREATE (v)-[:GSP_REVERSE {pair: 1}]->(u)"
        ),
        (
            f"UNWIND range(1, {scale}) AS i "
            f"CREATE (u:GspLow {{uid: {offset}+2*i, id: {offset}+i}})"
            f"-[:GSP_FORWARD {{pair: i+1}}]->(v:GspHigh {{uid: {offset}+2*i+1}}) "
            "CREATE (v)-[:GSP_REVERSE {pair: i+1}]->(u)"
        ),
    ]
    checks = [
        _index_check("GspLow", "id"),
        _count_check("MATCH (u:GspLow {id: 1}) RETURN count(u) AS count", 1),
        _count_check("MATCH (v:GspHigh) RETURN count(v) AS count", scale + 1),
    ]
    for forward, reverse in [("GSP_FORWARD", "GSP_REVERSE"), ("GSP_REVERSE", "GSP_FORWARD")]:
        checks.extend(
            [
                _count_check(f"MATCH ()-[r:{forward}]->() RETURN count(r) AS count", scale + 1),
                _count_check(
                    f"MATCH ()-[r:{forward}]->() RETURN count(DISTINCT r.pair) AS count", scale + 1
                ),
                _count_check(
                    (
                        f"MATCH (u)-[f:{forward}]->(v) "
                        f"OPTIONAL MATCH (v)-[b:{reverse}]->(u) WHERE b.pair = f.pair "
                        "WITH f, count(b) AS inverse_count WHERE inverse_count <> 1 "
                        "RETURN count(f) AS count"
                    ),
                    0,
                ),
            ]
        )
    query = Query(
        _edge(_node("u", "GspLow", "id"), "GSP_FORWARD", _node("v", "GspHigh")),
        None,
        _returns("u", "v"),
    )
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        [{"u.uid": 1, "v.uid": 2}],
        {
            "inverse_relationship": {
                "forward_type": "GSP_FORWARD",
                "reverse_type": "GSP_REVERSE",
                "pair_key": "pair",
            },
            "low_candidates": 1,
            "high_candidates": scale + 1,
        },
    )


def _chain_state(scale: int, seed: int) -> tuple[list[str], list[dict[str, Any]]]:
    offset = _offset(seed)
    setup = [
        _index("GspLow", "id"),
        "CREATE (:GspLow {uid: 1, id: 1})-[:GSP_LINK]->(:GspMiddle {uid: 2})-[:GSP_LINK]->(:GspHigh {uid: 3})",
        (
            f"UNWIND range(1, {scale}) AS i "
            f"CREATE (:GspLow {{uid: {offset}+3*i, id: {offset}+i}})"
            f"-[:GSP_LINK]->(:GspMiddle {{uid: {offset}+3*i+1}})"
            f"-[:GSP_LINK]->(:GspHigh {{uid: {offset}+3*i+2}})"
        ),
    ]
    checks = [
        _index_check("GspLow", "id"),
        _count_check("MATCH (u:GspLow {id: 1}) RETURN count(u) AS count", 1),
        _count_check("MATCH (v:GspHigh) RETURN count(v) AS count", scale + 1),
        _count_check("MATCH ()-[r:GSP_LINK]->() RETURN count(r) AS count", 2 * (scale + 1)),
    ]
    return setup, checks


def _build_p3(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    pruning_path = skeleton.source.get("state_variant") == "pruning_path"
    if pruning_path:
        offset = _offset(seed)
        setup = [
            _index("GspLow", "id"),
            "CREATE (u:GspLow {uid: 1, id: 1, keep: true})-[:GSP_LINK]->"
            "(:GspMiddle {uid: 2, keep: true})-[:GSP_LINK]->(:GspHigh {uid: 3, keep: true}) "
            "CREATE (u)-[:GSP_LINK]->(:GspBad {uid: 0, keep: false})",
            f"MATCH (b:GspBad {{uid: 0}}) UNWIND range(1, {scale}) AS i "
            f"CREATE (b)-[:GSP_LINK]->(:GspMiddle {{uid: {offset}+2*i, keep: true}})"
            f"-[:GSP_LINK]->(:GspHigh {{uid: {offset}+2*i+1, keep: true}})",
        ]
        checks = [
            _index_check("GspLow", "id"),
            _count_check("MATCH (u:GspLow {id: 1}) RETURN count(u) AS count", 1),
            _count_check("MATCH (v:GspHigh) RETURN count(v) AS count", scale + 1),
            _count_check("MATCH ()-[r:GSP_LINK]->() RETURN count(r) AS count", 2 * scale + 3),
            {"query": "MATCH (:GspLow {id: 1})-[:GSP_LINK]->(b:GspBad {uid: 0}) RETURN b.keep AS keep",
             "expected_rows": [{"keep": False}]},
            _count_check("MATCH (:GspBad {uid: 0})-[:GSP_LINK]->(m:GspMiddle) RETURN count(m) AS count", scale),
            {"query": "MATCH (u:GspLow {id: 1})-[:GSP_LINK]->(m:GspMiddle {uid: 2})-[:GSP_LINK]->(v:GspHigh {uid: 3}) "
                      "RETURN u.keep AS u_keep, m.keep AS m_keep, v.keep AS v_keep",
             "expected_rows": [{"u_keep": True, "m_keep": True, "v_keep": True}]},
        ]
        skeleton = replace(skeleton, text="MATCH p = $lowCostNode(u)-[:$R*$min..$max]-$highCostNode(v) "
                           "WHERE all(x IN nodes(p) WHERE x.keep = true) RETURN u.uid, v.uid, length(p) AS hops")
    else:
        setup, checks = _chain_state(scale, seed)
    path = Edge(
        _node("u", "GspLow", "id"),
        Relationship(type="GSP_LINK", min_hops=1, max_hops=3),
        _node("v", "GspHigh"),
        "undirected",
        path_variable="p",
    )
    predicate = All("x", Call("nodes", (Variable("p"),)), _eq("x", "keep", True)) if pruning_path else None
    query = Query(path, predicate, (*_returns("u", "v"), Alias(Call("length", (Variable("p"),)), "hops")))
    checks.append(
        _count_check(
            "MATCH (:GspLow {id: 1})-[:GSP_LINK]->(:GspMiddle)-[:GSP_LINK]->(:GspHigh) RETURN count(*) AS count",
            1,
        )
    )
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        [{"u.uid": 1, "v.uid": 3, "hops": 2}],
        {
            "min_hops": 1,
            "max_hops": 3,
            "low_candidates": 1,
            "high_candidates": scale + 1,
            "noise_chains": scale,
        },
    )


def _edge_state(scale: int, seed: int) -> tuple[list[str], list[dict[str, Any]]]:
    offset = _offset(seed)
    setup = [
        _index("GspLow", "id"),
        "CREATE (:GspLow {uid: 1, id: 1})-[:GSP_LINK]->(:GspHigh {uid: 2})",
        f"UNWIND range(1, {scale}) AS i CREATE (:GspLow {{uid: {offset}+2*i, id: {offset}+i}})-[:GSP_LINK]->(:GspHigh {{uid: {offset}+2*i+1}})",
    ]
    checks = [
        _index_check("GspLow", "id"),
        _count_check("MATCH (u:GspLow {id: 1}) RETURN count(u) AS count", 1),
        _count_check("MATCH (v:GspHigh) RETURN count(v) AS count", scale + 1),
    ]
    return setup, checks


def _build_p6(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    setup, checks = _edge_state(scale, seed)
    query = Query(
        _edge(_node("u", "GspLow"), "GSP_LINK", _node("v", "GspHigh")),
        _eq("u", "id", 1),
        _returns("u", "v"),
    )
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        [{"u.uid": 1, "v.uid": 2}],
        {"indexed_key": "id", "low_value": 1, "high_candidates": scale + 1},
    )


def _build_p7(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    offset = _offset(seed)
    setup = [
        _index("GspRare"),
        "CREATE (:GspRare:GspCommon {uid: 1})-[:GSP_LINK]->(:GspTarget {uid: 2})",
        f"UNWIND range(1, {scale}) AS i CREATE (:GspCommon {{uid: {offset}+2*i}})-[:GSP_LINK]->(:GspTarget {{uid: {offset}+2*i+1}})",
    ]
    query = Query(
        _edge(Node("u", ("GspRare", "GspCommon")), "GSP_LINK", _node("v", "GspTarget")),
        None,
        _returns("u", "v"),
    )
    checks = [
        _index_check("GspRare", "uid"),
        _count_check("MATCH (u:GspRare) RETURN count(u) AS count", 1),
        _count_check("MATCH (u:GspCommon) RETURN count(u) AS count", scale + 1),
        _count_check("MATCH (u:GspRare:GspCommon) RETURN count(u) AS count", 1),
    ]
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        [{"u.uid": 1, "v.uid": 2}],
        {
            "low_label": "GspRare",
            "high_label": "GspCommon",
            "low_candidates": 1,
            "high_candidates": scale + 1,
        },
    )


def _build_p8(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    offset = _offset(seed)
    setup = [
        _index("GspAnchor"),
        "CREATE (:GspAnchor {uid: 1})-[:GSP_LOW]->(:GspTarget {uid: 2, keep: true})",
        f"MATCH (u:GspAnchor {{uid: 1}}) UNWIND range(1, {4 * scale}) AS i CREATE (u)-[:GSP_HIGH]->(:GspTarget {{uid: {offset}+i, keep: false}})",
    ]
    pattern = Edge(
        _node("u", "GspAnchor", "uid"),
        Relationship(type="GSP_LOW", alternatives=("GSP_HIGH",)),
        _node("v", "GspTarget"),
    )
    query = Query(pattern, _eq("v", "keep", True), _returns("u", "v"))
    checks = [
        _index_check("GspAnchor", "uid"),
        _count_check("MATCH (u:GspAnchor {uid: 1}) RETURN count(u) AS count", 1),
        _count_check("MATCH (:GspAnchor {uid: 1})-[r:GSP_LOW]->() RETURN count(r) AS count", 1),
        _count_check(
            "MATCH (:GspAnchor {uid: 1})-[r:GSP_HIGH]->() RETURN count(r) AS count", 4 * scale
        ),
        _count_check(
            "MATCH (:GspAnchor {uid: 1})-[:GSP_HIGH]->(v) WHERE v.keep = true RETURN count(v) AS count",
            0,
        ),
    ]
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        [{"u.uid": 1, "v.uid": 2}],
        {"low_type": "GSP_LOW", "high_type": "GSP_HIGH", "high_candidates": 4 * scale},
    )


def _build_p9(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    setup, checks = _edge_state(scale, seed)
    query = Query(
        _node("u", "GspLow"),
        _eq("u", "id", 1),
        _returns("u", "v"),
        matches=(Match(_edge(Node("u"), "GSP_LINK", _node("v", "GspHigh"))),),
    )
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        [{"u.uid": 1, "v.uid": 2}],
        {
            "predicate_variable": "u",
            "indexed_key": "id",
            "low_value": 1,
            "high_candidates": scale + 1,
        },
    )


def _build_p10(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    offset = _offset(seed)
    empty_low = skeleton.source.get("state_variant") == "empty_low"
    low_id = 0 if empty_low else 1
    setup = [
        _index("GspLow", "id"),
        f"CREATE (:GspLow {{uid: 1, id: {low_id}}}), (:GspHigh {{uid: 2}})-[:GSP_LINK]->(:GspTarget {{uid: 3, keep: true}})",
        (
            f"UNWIND range(1, {scale}) AS i CREATE (b:GspHigh {{uid: {offset}+5*i}}) "
            "WITH b, i UNWIND range(1, 4) AS j "
            f"CREATE (b)-[:GSP_LINK]->(:GspTarget {{uid: {offset}+5*i+j, keep: false}})"
        ),
    ]
    high = Match(
        _edge(_node("b", "GspHigh"), "GSP_LINK", _node("c", "GspTarget")), _eq("c", "keep", True)
    )
    query = Query(_node("a", "GspLow", "id"), None, _returns("a", "b", "c"), matches=(high,))
    checks = [
        _index_check("GspLow", "id"),
        _count_check("MATCH (a:GspLow {id: 1}) RETURN count(a) AS count", 0 if empty_low else 1),
        _count_check(
            "MATCH (:GspHigh)-[r:GSP_LINK]->(:GspTarget) RETURN count(r) AS count", 4 * scale + 1
        ),
        _count_check(
            "MATCH (:GspHigh)-[:GSP_LINK]->(c:GspTarget) WHERE c.keep = true RETURN count(c) AS count",
            1,
        ),
    ]
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        [] if empty_low else [{"a.uid": 1, "b.uid": 2, "c.uid": 3}],
        {"independent_variables": [["a"], ["b", "c"]], "high_candidates": 4 * scale + 1},
    )


def _build_p11(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    offset = _offset(seed)
    empty_low = skeleton.source.get("state_variant") == "empty_low"
    multi_low = skeleton.source.get("state_variant") == "multi_low"
    low_uids = [2, *range(-1, -8, -1)] if multi_low else [2]
    first_create = ("CREATE (u:GspAnchor {uid: 1}) "
                    "CREATE (:GspAnchor {uid: 0})-[:GSP_LOW]->(:GspLow {uid: 2}) "
                    "CREATE (u)-[:GSP_HIGH]->(:GspHigh {uid: 3, keep: true})"
                    if empty_low else
                    "CREATE (u:GspAnchor {uid: 1})-[:GSP_LOW]->(:GspLow {uid: 2}) CREATE (u)-[:GSP_HIGH]->(:GspHigh {uid: 3, keep: true})")
    setup = [
        _index("GspAnchor"),
        first_create,
        f"MATCH (u:GspAnchor {{uid: 1}}) UNWIND range(1, {4 * scale}) AS i CREATE (u)-[:GSP_HIGH]->(:GspHigh {{uid: {offset}+i, keep: false}})",
    ]
    if multi_low:
        setup.append("MATCH (u:GspAnchor {uid: 1}) UNWIND range(1, 7) AS i "
                     "CREATE (u)-[:GSP_LOW]->(:GspLow {uid: -i})")
    query = Query(
        _node("u", "GspAnchor", "uid"),
        None,
        _returns("u", "v", "w"),
        matches=(
            Match(_edge(Node("u"), "GSP_LOW", _node("v", "GspLow"))),
            Match(_edge(Node("u"), "GSP_HIGH", _node("w", "GspHigh")), _eq("w", "keep", True)),
        ),
    )
    checks = [
        _index_check("GspAnchor", "uid"),
        _count_check("MATCH (u:GspAnchor {uid: 1}) RETURN count(u) AS count", 1),
        _count_check(
            "MATCH (:GspAnchor {uid: 1})-[:GSP_LOW]->(v:GspLow) RETURN count(v) AS count", 0 if empty_low else len(low_uids)
        ),
        _count_check(
            "MATCH (:GspAnchor {uid: 1})-[:GSP_HIGH]->(w:GspHigh) RETURN count(w) AS count",
            4 * scale + 1,
        ),
        _count_check(
            "MATCH (:GspAnchor {uid: 1})-[:GSP_HIGH]->(w:GspHigh) WHERE w.keep = true RETURN count(w) AS count",
            1,
        ),
    ]
    if empty_low:
        # A global LOW edge remains, so emptiness belongs to the bound uid=1
        # anchor rather than a globally absent relationship type.
        checks.append(_count_check("MATCH ()-[r:GSP_LOW]->() RETURN count(r) AS count", 1))
    elif multi_low:
        checks.append(_count_check("MATCH (v:GspLow) RETURN count(DISTINCT v.uid) AS count", 8))
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        [] if empty_low else [{"u.uid": 1, "v.uid": uid, "w.uid": 3} for uid in low_uids],
        {"shared_anchor": "u", "low_candidates": 0 if empty_low else len(low_uids), "high_candidates": 4 * scale + 1},
    )


def _build_p12(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    setup, checks = _chain_state(scale, seed)
    query = Query(
        _edge(_node("u", "GspLow", "id"), "GSP_LINK", _node("v", "GspMiddle")),
        None,
        _returns("u", "w"),
        matches=(Match(_edge(Node("v"), "GSP_LINK", _node("w", "GspHigh"))),),
    )
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        [{"u.uid": 1, "w.uid": 3}],
        {"shared_variable": "v", "low_candidates": 1, "high_candidates": scale + 1},
    )


def _build_p13(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    offset = _offset(seed)
    multi_low = skeleton.source.get("state_variant") == "multi_low"
    low_uids = [10, *range(-1, -8, -1)] if multi_low else [10]
    setup = [
        _index("GspAnchor"),
        (
            "CREATE (a:GspAnchor {uid: 1})-[:GSP_LOW]->(:GspLow {uid: 10}) "
            "CREATE (a)-[:GSP_HIGH]->(:GspHigh {uid: 20, keep: true}) "
            "CREATE (:GspAnchor {uid: 2})-[:GSP_LOW]->(:GspLow {uid: 11}) "
            "CREATE (:GspAnchor {uid: 3})-[:GSP_HIGH]->(:GspHigh {uid: 21, keep: true})"
        ),
        f"MATCH (u:GspAnchor {{uid: 1}}) UNWIND range(1, {4 * scale}) AS i CREATE (u)-[:GSP_HIGH]->(:GspHigh {{uid: {offset}+i, keep: false}})",
    ]
    if multi_low:
        setup.append("MATCH (u:GspAnchor {uid: 1}) UNWIND range(1, 7) AS i "
                     "CREATE (u)-[:GSP_LOW]->(:GspLow {uid: -i})")
    query = Query(
        _node("u", "GspAnchor"),
        None,
        _returns("u", "v", "w"),
        matches=(
            Match(_edge(Node("u"), "GSP_LOW", _node("v", "GspLow")), optional=True),
            Match(
                _edge(Node("u"), "GSP_HIGH", _node("w", "GspHigh")),
                _eq("w", "keep", True),
                optional=True,
            ),
        ),
    )
    checks = [
        _index_check("GspAnchor", "uid"),
        _count_check("MATCH (u:GspAnchor) RETURN count(u) AS count", 3),
        _count_check("MATCH (:GspAnchor)-[:GSP_LOW]->(v:GspLow) RETURN count(v) AS count", len(low_uids) + 1),
        _count_check(
            "MATCH (:GspAnchor)-[:GSP_HIGH]->(w:GspHigh) RETURN count(w) AS count", 4 * scale + 2
        ),
        _count_check(
            "MATCH (:GspAnchor)-[:GSP_HIGH]->(w:GspHigh) WHERE w.keep = true RETURN count(w) AS count",
            2,
        ),
    ]
    if multi_low:
        checks.extend([
            _count_check("MATCH (:GspAnchor {uid: 1})-[:GSP_LOW]->(v:GspLow) RETURN count(v) AS count", 8),
            _count_check("MATCH (:GspAnchor {uid: 2})-[:GSP_LOW]->(v:GspLow) RETURN count(v) AS count", 1),
            _count_check("MATCH (:GspAnchor {uid: 3})-[:GSP_LOW]->(v:GspLow) RETURN count(v) AS count", 0),
            _count_check("MATCH (:GspAnchor {uid: 2})-[:GSP_HIGH]->(w:GspHigh) WHERE w.keep = true RETURN count(w) AS count", 0),
            _count_check("MATCH (:GspAnchor {uid: 3})-[:GSP_HIGH]->(w:GspHigh) WHERE w.keep = true RETURN count(w) AS count", 1),
            _count_check("MATCH (v:GspLow) RETURN count(DISTINCT v.uid) AS count", 9),
        ])
    rows = [{"u.uid": 1, "v.uid": uid, "w.uid": 20} for uid in low_uids] + [
        {"u.uid": 2, "v.uid": 11, "w.uid": None},
        {"u.uid": 3, "v.uid": None, "w.uid": 21},
    ]
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        rows,
        {
            "shared_anchor": "u",
            "branch_local_variables": ["v", "w"],
            "anchor_cases": ["both", "low_only", "high_only"],
            "high_candidates": 4 * scale + 2,
        },
    )


def _build_p14(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    offset = _offset(seed)
    setup = [
        _index("GspLow"),
        "CREATE (:GspLow {uid: 1}), (:GspHigh {uid: 2, keep: true})",
        f"UNWIND range(1, {4 * scale}) AS i CREATE (:GspHigh {{uid: {offset}+i, keep: false}})",
    ]
    low = Query(_node("a", "GspLow"), None, (Alias(Property("a", "uid"), "uid"),))
    high = Query(
        _node("b", "GspHigh"), _eq("b", "keep", True), (Alias(Property("b", "uid"), "uid"),)
    )
    query = UnionAll((low, high))
    checks = [
        _index_check("GspLow", "uid"),
        _count_check("MATCH (a:GspLow) RETURN count(a) AS count", 1),
        _count_check("MATCH (b:GspHigh) RETURN count(b) AS count", 4 * scale + 1),
        _count_check("MATCH (b:GspHigh) WHERE b.keep = true RETURN count(b) AS count", 1),
    ]
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        [{"uid": 1}, {"uid": 2}],
        {"union_mode": "all", "projection_aliases": ["uid"], "high_candidates": 4 * scale + 1},
    )


def _build_p15(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    offset = _offset(seed)
    setup = [
        _index("GspAnchor"),
        (
            "CREATE (:GspAnchor {uid: 1, keep: true}), "
            "(:GspAnchor {uid: 2, keep: false})-[:GSP_EXPAND]->"
            "(:GspLeaf {uid: 4, mark: true}), (:GspAnchor {uid: 3})"
        ),
        f"MATCH (u:GspAnchor {{uid: 1}}) UNWIND range(1, {4 * scale}) AS i CREATE (u)-[:GSP_EXPAND]->(:GspLeaf {{uid: {offset}+i, mark: false}})",
    ]
    expensive = Exists(
        _edge(Node("u"), "GSP_EXPAND", _node("x", "GspLeaf")), _eq("x", "mark", True)
    )
    query = Query(
        _node("u", "GspAnchor"),
        None,
        (
            Property("u", "uid"),
            Alias(Case(_eq("u", "keep", True), Literal(True), expensive), "value"),
        ),
    )
    checks = [
        _index_check("GspAnchor", "uid"),
        {
            "query": "MATCH (u:GspAnchor) RETURN u.uid AS uid, u.keep AS keep",
            "expected_rows": [
                {"uid": 1, "keep": True},
                {"uid": 2, "keep": False},
                {"uid": 3, "keep": None},
            ],
        },
        _count_check(
            "MATCH (:GspAnchor {uid: 1})-[:GSP_EXPAND]->(x:GspLeaf) RETURN count(x) AS count",
            4 * scale,
        ),
        _count_check(
            "MATCH (:GspAnchor {uid: 1})-[:GSP_EXPAND]->(x:GspLeaf) WHERE x.mark = true RETURN count(x) AS count",
            0,
        ),
        _count_check(
            "MATCH (:GspAnchor {uid: 2})-[:GSP_EXPAND]->(x:GspLeaf) WHERE x.mark = true RETURN count(x) AS count",
            1,
        ),
        _count_check(
            "MATCH (:GspAnchor {uid: 3})-[:GSP_EXPAND]->(x:GspLeaf) RETURN count(x) AS count", 0
        ),
    ]
    rows = [{"u.uid": 1, "value": True}, {"u.uid": 2, "value": True}, {"u.uid": 3, "value": False}]
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        setup,
        checks,
        rows,
        {
            "condition_values": [True, False, None],
            "high_candidates": 4 * scale,
            "null_takes_else": True,
        },
    )


def _build_p16(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    items = Concat(ListExpr((Literal(0),)), Call("range", (Literal(1), Literal(4 * scale))))
    expression = All("x", items, Comparison(Variable("x"), ">", Literal(0)))
    query = Query(None, None, (Alias(expression, "ok"),))
    checks = [
        _count_check(f"RETURN size(range(1, {4 * scale})) AS count", 4 * scale),
        {"query": "RETURN all(x IN [0] WHERE x > 0) AS ok", "expected_rows": [{"ok": False}]},
    ]
    return _pack(
        skeleton,
        scale,
        seed,
        query,
        [],
        checks,
        [{"ok": False}],
        {
            "low_list": [0],
            "high_list_range": [1, 4 * scale],
            "predicate": "x > 0",
            "pruning_scope": "quantifier predicate evaluation only; list construction remains required",
        },
    )


def _scor_state(scale: int, seed: int) -> tuple[list[str], list[dict[str, Any]]]:
    """Keep large and reference regions in the same checked database state."""
    offset = _offset(seed)
    setup = [
        _index("ScorRoot", "id"),
        f"CREATE (:ScorRoot {{id: 1, uid: 1, key: 1, expected_count: {4 * scale}}})",
        (
            "MATCH (u:ScorRoot {id: 1}) "
            f"UNWIND range(1, {4 * scale}) AS i "
            "CREATE (u)-[:GSP_SC_HIGH]->"
            f"(:ScorTarget {{uid: {offset}+i, mark: true, key: 1}})"
        ),
        (
            "MATCH (u:ScorRoot {id: 1}) UNWIND range(2, 4) AS i "
            "CREATE (u)-[:GSP_SC_REF]->(:ScorTarget {uid: i, mark: true, key: 1})"
        ),
    ]
    checks = [
        _index_check("ScorRoot", "id"),
        _count_check("MATCH (u:ScorRoot) RETURN count(u) AS count", 1),
        {
            "query": "MATCH (u:ScorRoot {id: 1}) RETURN u.uid AS uid, u.key AS key, u.expected_count AS expected_count",
            "expected_rows": [{"uid": 1, "key": 1, "expected_count": 4 * scale}],
        },
    ]
    for relation_type, count in (("GSP_SC_HIGH", 4 * scale), ("GSP_SC_REF", 3)):
        checks.extend(
            [
                _count_check(
                    f"MATCH (:ScorRoot {{id: 1}})-[r:{relation_type}]->(:ScorTarget) "
                    "RETURN count(r) AS count",
                    count,
                ),
                _count_check(
                    f"MATCH (u:ScorRoot {{id: 1}})-[:{relation_type}]->(v:ScorTarget) "
                    "WHERE v.mark = true AND v.key = u.key RETURN count(v) AS count",
                    count,
                ),
            ]
        )
    checks.append(
        _count_check(
            "MATCH (v:ScorTarget) RETURN count(DISTINCT v.uid) AS count",
            4 * scale + 3,
        )
    )
    return setup, checks


def _scor_pattern(*, correlated: bool = False) -> Edge:
    root = Node("u") if correlated else _node("u", "ScorRoot", "id")
    return _edge(root, "GSP_SC_HIGH", _node("v", "ScorTarget"))


def _scor_count(*, correlated: bool = True) -> Count:
    return Count(_scor_pattern(correlated=correlated), _eq("v", "mark", True))


def _scor_scalar(expression: Any) -> Query:
    return Query(_node("u", "ScorRoot", "id"), None, (Alias(expression, "value"),))


def _scor_s1(seed: int) -> tuple[Query, list[dict[str, Any]], dict[str, Any]]:
    # Exact filtered counts exercise traversal work rather than mere existence.
    expression = Boolean("OR", (Literal(True), Comparison(_scor_count(), "=", Property("u", "expected_count"))))
    return _scor_scalar(expression), [{"value": True}], {"decisive_value": True}


def _scor_s2(seed: int) -> tuple[Query, list[dict[str, Any]], dict[str, Any]]:
    expression = Boolean("AND", (Literal(False), Comparison(_scor_count(), "=", Property("u", "expected_count"))))
    return _scor_scalar(expression), [{"value": False}], {"decisive_value": False}


def _scor_s3(seed: int) -> tuple[ConditionalQuery, list[dict[str, Any]], dict[str, Any]]:
    selected = Query(None, None, (Alias(Literal(7), "value"),))
    unselected = Query(None, None, (Alias(_scor_count(correlated=False), "value"),))
    return (
        ConditionalQuery(Literal(True), selected, unselected),
        [{"value": 7}],
        {
            "condition": True,
            "selected_branch": "then",
            "required_cypher": 25,
        },
    )


def _scor_s4(seed: int) -> tuple[Query, list[dict[str, Any]], dict[str, Any]]:
    condition = (True, False, None)[seed % 3]
    then, otherwise = (
        (Literal(7), _scor_count()) if condition is True else (_scor_count(), Literal(7))
    )
    expression = Case(Literal(condition), then, otherwise)
    return (
        _scor_scalar(expression),
        [{"value": 7}],
        {
            "condition": condition,
            "selected_branch": "then" if condition is True else "else",
        },
    )


def _scor_s5(seed: int) -> tuple[Query, list[dict[str, Any]], dict[str, Any]]:
    false_filter = bool(seed % 2)
    query = Query(
        _scor_pattern(),
        Literal(False) if false_filter else None,
        (Alias(Literal(1), "value"),),
        limit=None if false_filter else 0,
    )
    return query, [], {"variant": "where_false" if false_filter else "limit_zero"}


def _scor_s6(seed: int) -> tuple[Query, list[dict[str, Any]], dict[str, Any]]:
    limit = 1 + seed % 3
    query = Query(_scor_pattern(), None, (Alias(Literal(1), "value"),), limit=limit)
    return (
        query,
        [{"value": 1} for _ in range(limit)],
        {
            "limit": limit,
            "constant_projection": True,
        },
    )


def _scor_s7(seed: int) -> tuple[Query, list[dict[str, Any]], dict[str, Any]]:
    expression = Call("coalesce", (Literal(None), Literal(0), _scor_count()))
    return _scor_scalar(expression), [{"value": 0}], {"first_nonnull_value": 0}


def _scor_s8(seed: int) -> tuple[Query, list[dict[str, Any]], dict[str, Any]]:
    condition = Comparison(Property("v", "key"), "=", Property("u", "key"))
    query = Query(
        _node("u", "ScorRoot", "id"),
        Exists(_scor_pattern(correlated=True), condition),
        (Property("u", "uid"),),
    )
    return query, [{"u.uid": 1}], {"all_candidates_match": True}


def _scor_s9(seed: int) -> tuple[Query, list[dict[str, Any]], dict[str, Any]]:
    query = Query(
        _node("e", "ScorRoot"),
        Literal(False),
        (Alias(Literal(1), "value"),),
        matches=(Match(_scor_pattern()),),
    )
    return query, [], {"empty_upstream": True, "independent_variables": [["e"], ["u", "v"]]}


def _scor_s10(seed: int) -> tuple[Query, list[dict[str, Any]], dict[str, Any]]:
    null_safe_equality = Boolean(
        "OR",
        (
            Comparison(Property("u", "key"), "=", Property("v", "key")),
            Boolean(
                "AND",
                (
                    Comparison(Property("u", "key"), "IS NULL", Literal(None)),
                    Comparison(Property("v", "key"), "IS NULL", Literal(None)),
                ),
            ),
        ),
    )
    membership = Exists(_scor_pattern(correlated=True), null_safe_equality)
    difference = bool(seed % 2)
    query = Query(
        _node("u", "ScorRoot", "id"),
        Boolean("AND", (Literal(False), Negation(membership) if difference else membership)),
        (Alias(Property("u", "key"), "key"),),
        distinct=True,
    )
    return (
        query,
        [],
        {
            "set_operation": "empty_except" if difference else "empty_intersection",
            "native_set_operator": False,
            "null_safe_membership": True,
        },
    )


_SCOR_BUILDERS = {
    "S1": _scor_s1,
    "S2": _scor_s2,
    "S3": _scor_s3,
    "S4": _scor_s4,
    "S5": _scor_s5,
    "S6": _scor_s6,
    "S7": _scor_s7,
    "S8": _scor_s8,
    "S9": _scor_s9,
    "S10": _scor_s10,
}


def _build_scor(skeleton: Skeleton, scale: int, seed: int) -> dict[str, Any]:
    setup, checks = _scor_state(scale, seed)
    query, rows, details = _SCOR_BUILDERS[skeleton.pattern](skeleton.source.get("population_seed", seed))
    contract = {
        "pattern": skeleton.pattern,
        "paper_pattern": _SCOR_SPECS[skeleton.pattern][0],
        "high_type": "GSP_SC_HIGH",
        "reference_type": "GSP_SC_REF",
        "root_label": "ScorRoot",
        "target_label": "ScorTarget",
        "root_key": "id",
        "root_value": 1,
        "match_key": "key",
        "high_matches": 4 * scale,
        "reference_matches": 3,
        "assumptions": [
            "Both regions belong to the same uniquely indexed root.",
            "Every candidate has mark=true and key equal to the root key.",
            "Only the declared high-cost relationship type may be replaced.",
        ],
        **details,
    }
    case = _pack(skeleton, scale, seed, query, setup, checks, rows, {"scor": contract})
    if skeleton.pattern == "S3":
        case["required_cypher"] = 25
    elif skeleton.pattern == "S4" and details["condition"] is not True:
        case["skeleton"] = (
            "RETURN CASE WHEN $lowCostBool THEN $highCostExpr(u) ELSE $lowCostExpr END AS value"
        )
    elif skeleton.pattern == "S5" and details["variant"] == "where_false":
        case["skeleton"] = "MATCH $highCostPattern(u,v) WHERE $lowCostBool RETURN 1 AS value"
        case["placeholders"][0] = {
            "name": "lowCostBool",
            "kind": "boolean_expression",
            "role": "low",
            "variables": [],
        }
    return case


_BUILDERS = {
    "P1": _build_initial,
    "P2": _build_p2,
    "P3": _build_p3,
    "P4": _build_initial,
    "P5": _build_initial,
    "P6": _build_p6,
    "P7": _build_p7,
    "P8": _build_p8,
    "P9": _build_p9,
    "P10": _build_p10,
    "P11": _build_p11,
    "P12": _build_p12,
    "P13": _build_p13,
    "P14": _build_p14,
    "P15": _build_p15,
    "P16": _build_p16,
    **{pattern: _build_scor for pattern in _SCOR_BUILDERS},
}


def _walk_replace(value: Any, change) -> Any:
    """Small structural mapper for the existing AST, not a new query grammar."""
    if isinstance(value, tuple):
        value = tuple(_walk_replace(item, change) for item in value)
    elif is_dataclass(value):
        value = replace(value, **{field.name: _walk_replace(getattr(value, field.name), change)
                                 for field in fields(value)})
    return change(value)


def _flag_forms(variable: str, key: str) -> tuple[Any, ...]:
    flag = Property(variable, key)
    return (Comparison(flag, "=", Literal(True)),
            Negation(Comparison(flag, "=", Literal(False))),
            Comparison(Call("coalesce", (flag, Literal(False))), "=", Literal(True)))


def _constant_forms(value: bool | None) -> tuple[Any, ...]:
    if value is None:
        return (Literal(None), Negation(Literal(None)), Comparison(Literal(None), "=", Literal(True)))
    return (Literal(value), Comparison(Literal(2), ">" if value else "<", Literal(1)),
            Negation(Literal(not value)))


def _populate_variants(case: dict[str, Any], seed: int, complexity: int = 0) -> None:
    """Finite role pools modeled after SCor's fixed candidate lists.

    Choices use only facts already visible in these constructed graphs: uid=1
    and id=1 denote the same anchor, boolean properties are actual booleans,
    and the bounded-path witness has two hops. No answer labels are added.
    """
    pattern, query = case["pattern"], case["query_a"]
    selected: dict[str, Any] = {}
    role_choices: dict[str, Any] = {}
    if case["source"].get("state_variant") in {"empty_low", "multi_low", "pruning_path"}:
        selected["state_variant"] = role_choices["state_variant"] = case["source"]["state_variant"]

    def pick(name, options, divisor=1):
        if complexity:
            role_seed = int.from_bytes(sha256(f"{seed}:{pattern}:{name}".encode()).digest()[:8], "big")
            index = random.Random(role_seed).randrange(len(options))
        else:
            index = seed // divisor % len(options)
        selected[name] = index
        option = options[index]
        role_choices[name] = (render_expression(option) if is_dataclass(option)
                              else list(option) if isinstance(option, tuple) else option)
        return option

    def drop(name):
        selected.pop(name, None)
        role_choices.pop(name, None)

    if pattern.startswith("P"):
        parts = list(clauses(query)) if isinstance(query, Query) else []
        if pattern in {"P1", "P2", "P3", "P10", "P12"}:
            placement = pick("anchor_constraint", ("map", "where"))
            if placement == "where":
                first = parts[0]
                node = first.pattern.left if isinstance(first.pattern, Edge) else first.pattern
                key, value = node.properties[0]
                bare = replace(node, properties=())
                graph = replace(first.pattern, left=bare) if isinstance(first.pattern, Edge) else bare
                moved = Comparison(Property(node.variable, key), "=", value)
                predicate = Boolean("AND", (moved, first.where)) if first.where is not None else moved
                parts[0] = replace(first, pattern=graph, where=predicate)
        if pattern == "P3":
            bounds_pool = ((1, 3),) if case["source"].get("state_variant") == "pruning_path" else ((1, 3), (2, 2), (1, 2), (2, 3))
            bounds = pick("path_bounds", bounds_pool, 2)
            edge = parts[0].pattern
            parts[0] = replace(parts[0], pattern=replace(edge, relationship=replace(
                edge.relationship, min_hops=bounds[0], max_hops=bounds[1])))
            case["bindings"].update(min_hops=bounds[0], max_hops=bounds[1])
        elif pattern in {"P4", "P5"}:
            low, high = parts[0].where.terms
            variable = parts[0].pattern.variable
            low = pick("decisive_predicate", _flag_forms(variable, "keep"))
            leaf = high.pattern.right.variable
            high = replace(high, where=pick("high_predicate", _flag_forms(leaf, "mark"), 3))
            kind = pick("high_operator", ("exists", "count"), 9)
            if kind == "count":
                high = Comparison(Count(high.pattern, high.where), ">", Literal(0))
            parts[0] = replace(parts[0], where=Boolean(parts[0].where.operator, (low, high)))
        elif pattern == "P6":
            if pick("comparison_order", ("property_first", "literal_first")) == "literal_first":
                predicate = parts[0].where
                parts[0] = replace(parts[0], where=Comparison(predicate.right, "=", predicate.left))
            if pick("edge_spelling", ("out", "in"), 2) == "in":
                edge = parts[0].pattern
                parts[0] = replace(parts[0], pattern=replace(edge, left=edge.right, right=edge.left, direction="in"))
        elif pattern == "P7":
            if pick("anchor_filter", ("label_only", "uid_equality")) == "uid_equality":
                parts[0] = replace(parts[0], where=_eq("u", "uid", 1))
            if pick("label_order", ("rare_first", "common_first"), 2) == "common_first":
                edge = parts[0].pattern
                parts[0] = replace(parts[0], pattern=replace(edge, left=replace(edge.left, labels=tuple(reversed(edge.left.labels)))))
        elif pattern == "P8":
            parts[0] = replace(parts[0], where=pick("high_predicate", _flag_forms("v", "keep")))
            if pick("type_order", ("low_first", "high_first"), 3) == "high_first":
                edge = parts[0].pattern
                parts[0] = replace(parts[0], pattern=replace(edge, relationship=replace(
                    edge.relationship, type="GSP_HIGH", alternatives=("GSP_LOW",))))
        elif pattern == "P9":
            predicate = parts[0].where
            if pick("comparison_order", ("property_first", "literal_first")) == "literal_first":
                predicate = Comparison(predicate.right, "=", predicate.left)
            if pick("predicate_attachment", ("first", "second"), 2) == "second":
                parts[0], parts[1] = replace(parts[0], where=None), replace(parts[1], where=predicate)
            else:
                parts[0] = replace(parts[0], where=predicate)
        elif pattern == "P15":
            projection = query.returns[1]
            expression = projection.expression
            cheap = pick("decisive_predicate", _flag_forms("u", "keep"))
            high = replace(expression.otherwise, where=pick("high_predicate", _flag_forms("x", "mark"), 3))
            if pick("high_operator", ("exists", "count"), 9) == "count":
                high = Comparison(Count(high.pattern, high.where), ">", Literal(0))
            query = replace(query, returns=(query.returns[0], replace(projection, expression=Case(cheap, Literal(True), high))))
        elif pattern == "P16":
            expression = query.returns[0].expression
            low = pick("decisive_list", (ListExpr((Literal(0),)), ListExpr((Literal(0), Literal(0)))))
            maximum = case["bindings"]["high_list_range"][1]
            high = pick("range_direction", (Call("range", (Literal(1), Literal(maximum))),
                                             Call("range", (Literal(maximum), Literal(1), Literal(-1)))), 2)
            predicate = pick("quantifier_predicate", (Comparison(Variable("x"), ">", Literal(0)),
                                                       Comparison(Variable("x"), "<>", Literal(0))), 4)
            expression = replace(expression, items=Concat(low, high), predicate=predicate)
            query = replace(query, returns=(replace(query.returns[0], expression=expression),))
            case["bindings"]["low_list"] = [item.value for item in low.items]
            case["bindings"]["predicate"] = "x > 0" if predicate.operator == ">" else "x <> 0"
        if pattern in {"P10", "P11", "P13"}:
            index = 1 if pattern == "P10" else 2
            variable = "c" if pattern == "P10" else "w"
            parts[index] = replace(parts[index], where=pick("high_predicate", _flag_forms(variable, "keep"), 2))
        if pattern in {"P11", "P13"} and pick("branch_order", ("low_first", "high_first"), 6) == "high_first":
            parts[1], parts[2] = parts[2], parts[1]
        if pattern == "P12" and pick("match_order", ("forward", "backward"), 2) == "backward":
            parts.reverse()
        if pattern == "P14":
            low, high = query.branches
            high = replace(high, where=pick("high_predicate", _flag_forms("b", "keep")))
            branches = (low, high) if pick("branch_order", ("low_first", "high_first"), 3) == "low_first" else (high, low)
            query = replace(query, branches=branches)
        elif parts:
            query = with_clauses(query, tuple(parts))
    else:
        spec = case["bindings"]["scor"]
        for name in ("condition", "variant", "limit", "set_operation"):
            if name in spec:
                selected[name] = spec[name]
        # Root id and uid are both 1 in the checked singleton root; this choice
        # does not change the state and exercises different lookup spellings.
        root_key = pick("root_key", ("id", "uid"), 9)
        if root_key == "uid":
            query = _walk_replace(query, lambda value: replace(value, properties=(("uid", Literal(1)),))
                                  if isinstance(value, Node) and value.labels == ("ScorRoot",)
                                  and value.properties == (("id", Literal(1)),) else value)
            spec["root_key"] = "uid"
        if pattern in {"S1", "S2"}:
            projection = query.returns[0]
            expression = projection.expression
            cheap = pick("decisive_expression", _constant_forms(pattern == "S1"), 3)
            expensive = expression.terms[1]
            if pick("high_operator", ("count_equals", "count_positive", "exists"), 9) != "count_equals":
                count = expensive.left
                expensive = (Comparison(count, ">", Literal(0)) if selected["high_operator"] == 1
                             else Exists(count.pattern, count.where))
            query = replace(query, returns=(replace(projection, expression=replace(expression, terms=(cheap, expensive))),))
        elif pattern == "S3":
            query = replace(query, condition=pick("decisive_expression", _constant_forms(True), 3))
        elif pattern == "S4":
            projection = query.returns[0]
            expression = projection.expression
            condition = pick("condition_expression", _constant_forms(spec["condition"]), 3)
            query = replace(query, returns=(replace(projection, expression=replace(expression, condition=condition)),))
        elif pattern in {"S5", "S9"}:
            if query.where is not None:
                query = replace(query, where=pick("empty_guard", _constant_forms(False), 3))
        elif pattern == "S6":
            value = pick("constant_projection", (1, 0, "value"), 3)
            query = replace(query, returns=(Alias(Literal(value), "value"),))
            case["expected_rows"] = [{"value": value}] * query.limit
        elif pattern == "S7":
            value = pick("nonnull_prefix", (0, False, "", 7), 3)
            projection = query.returns[0]
            expression = projection.expression
            query = replace(query, returns=(replace(projection, expression=replace(expression, args=(Literal(None), Literal(value), expression.args[-1]))),))
            case["expected_rows"] = [{"value": value}]
            spec["first_nonnull_value"] = value
        elif pattern == "S8":
            exists = query.where
            predicate = pick("witness_predicate", (exists.where, None, Literal(True),
                              Comparison(Property("u", "key"), "=", Property("v", "key"))), 3)
            query = replace(query, where=replace(exists, where=predicate))
        elif pattern == "S10":
            guard, membership = query.where.terms
            guard = pick("empty_guard", _constant_forms(False), 3)
            query = replace(query, where=replace(query.where, terms=(guard, membership)))
        if pattern in {"S1", "S2", "S3", "S4", "S7"}:
            form = pick("count_predicate", ("mark_true", "not_mark_false", "unfiltered"), 27)
            if form != "mark_true":
                query = _walk_replace(query, lambda value: replace(value, where=(
                    Negation(_eq(value.pattern.right.variable, "mark", False)) if form == "not_mark_false" else None))
                    if isinstance(value, (Count, Exists)) and value.pattern.relationship.type == "GSP_SC_HIGH" else value)
        case["reference_side"] = "b"
    if complexity:
        selected["complexity"] = complexity
        query = _expand_predicates(case, query, complexity, pick, drop)
    case["query_a"] = query
    case["complexity"] = complexity
    case["variant"] = selected
    case["role_choices"] = role_choices
    case["bindings"]["variant"] = selected.copy()
    case["bindings"]["role_choices"] = role_choices.copy()
    signature = json.dumps(selected, sort_keys=True, separators=(",", ":"))
    case["template_id"] = pattern + "-" + sha256(signature.encode()).hexdigest()[:10]
    case["source"].update(template_id=case["template_id"], variant=selected.copy(), role_choices=role_choices.copy(),
                          complexity=complexity, role_sampling="independent_seeded_random" if complexity else "finite_enumeration")


def _nested_boolean(expression, gate, depth, pick, role):
    """Equivalent nullable predicates under a checked non-null gate.

    Each gate is true for this fixture. COALESCE with NULL and double NOT
    preserve all three truth values even without that gate assumption.
    """
    for level in range(depth):
        expression = pick(f"{role}_nest_{level}", (
            Negation(Negation(expression)),
            Call("coalesce", (expression, Literal(None))),
            Boolean("AND", (expression, gate)),
            Boolean("OR", (expression, Negation(gate))),
            Case(gate, expression, Literal(None)),
        ))
    return expression


def _expand_predicates(case, query, complexity, pick, drop):
    """Bounded nested expressions; no new grammar or unconstrained solver."""
    pattern = case["pattern"]
    if pattern.startswith("S"):
        spec = case["bindings"]["scor"]
        guard_check = {
            "query": "MATCH (u:ScorRoot {id: 1}) RETURN u.id AS id, u.uid AS uid, u.key AS key",
            "expected_rows": [{"id": 1, "uid": 1, "key": 1}],
        }
        cardinality_check = _count_check("MATCH (u:ScorRoot) RETURN count(u) AS count", 1)
        case["checks"].append(guard_check)
        spec.update(guard_facts={"id": 1, "uid": 1, "key": 1}, guard_check=guard_check,
                    guard_cardinality_check=cardinality_check)
        # WHEN has no outer binding, so its cheap dynamic guard reads only the
        # checked singleton root. Ordinary queries use their bound root key.
        if pattern == "S3":
            root = Node("guard", ("ScorRoot",), ((spec["root_key"], Literal(1)),))
            cheap_count = Count(root, _eq("guard", "key", 1))
            gate = Comparison(cheap_count, ">", Literal(0))
        else:
            variable = "e" if pattern == "S9" else "u"
            gate = _eq(variable, "key", 1)
        if pattern in {"S1", "S2", "S4", "S9", "S10"} or pattern == "S5" and query.where is not None:
            drop("decisive_expression" if pattern in {"S1", "S2"} else "condition_expression" if pattern == "S4" else "empty_guard")
            if pattern == "S4":
                expected = spec["condition"]
            else:
                expected = pattern == "S1"
            dynamic = gate if expected is True else Negation(gate) if expected is False else Case(gate, Literal(None), Literal(False))
            dynamic = _nested_boolean(dynamic, gate, complexity, pick, "root_guard")
            if pattern in {"S1", "S2"}:
                projection = query.returns[0]
                expression = projection.expression
                query = replace(query, returns=(replace(projection, expression=replace(expression,
                    terms=(dynamic, expression.terms[1]))),))
            elif pattern == "S4":
                projection = query.returns[0]
                query = replace(query, returns=(replace(projection, expression=replace(projection.expression,
                    condition=dynamic)),))
            elif pattern in {"S5", "S9"} and query.where is not None:
                query = replace(query, where=dynamic)
            elif pattern == "S10":
                query = replace(query, where=replace(query.where, terms=(dynamic, query.where.terms[1])))
        elif pattern == "S3":
            drop("decisive_expression")
            query = replace(query, condition=_nested_boolean(gate, gate, complexity, pick, "root_guard"))
        elif pattern == "S7":
            drop("nonnull_prefix")
            prefix = pick("dynamic_nonnull_prefix", (
                Property("u", "key"), Call("coalesce", (Property("u", "uid"), Literal(None))),
                Case(gate, Property("u", "uid"), Literal(None)), gate,
            ))
            prefix_value = True if prefix == gate else 1
            for level in range(complexity):
                prefix = pick(f"nonnull_nest_{level}", (
                    Call("coalesce", (prefix, Literal(None))), Case(gate, prefix, Literal(None)),
                ))
            projection = query.returns[0]
            query = replace(query, returns=(replace(projection, expression=replace(projection.expression,
                args=(Literal(None), prefix, projection.expression.args[-1]))),))
            # The checked key/uid are 1; only the equality candidate is boolean.
            case["expected_rows"] = [{"value": prefix_value}]
            spec["first_nonnull_value"] = prefix_value
        if pattern in {"S1", "S2", "S3", "S4", "S7"}:
            def correlate(value):
                if (isinstance(value, (Count, Exists)) and isinstance(value.pattern, Edge)
                        and value.pattern.relationship.type == "GSP_SC_HIGH"):
                    edge = value.pattern
                    correlated = Comparison(Property(edge.right.variable, "key"), "=", Property(edge.left.variable, "key"))
                    base = value.where or Literal(True)
                    combined = Boolean("AND", (base, correlated))
                    gate = Comparison(Property(edge.left.variable, "key"), "=", Literal(1))
                    return replace(value, where=_nested_boolean(combined, gate, complexity, pick, "high_correlated"))
                return value
            query = _walk_replace(query, correlate)
    elif pattern in {"P4", "P5", "P15"}:
        variable = query.pattern.variable
        gate = Comparison(Property(variable, "uid"), "=", Property(variable, "uid"))
        if pattern in {"P4", "P5"}:
            low, high = query.where.terms
            query = replace(query, where=replace(query.where,
                terms=(_nested_boolean(low, gate, complexity, pick, "cheap_predicate"), high)))
        else:
            projection = query.returns[1]
            query = replace(query, returns=(query.returns[0], replace(projection, expression=replace(
                projection.expression, condition=_nested_boolean(projection.expression.condition,
                    gate, complexity, pick, "cheap_predicate")))))
        query = _walk_replace(query, lambda value: replace(value, where=_nested_boolean(
            value.where, gate, complexity, pick, "high_predicate"))
            if isinstance(value, (Count, Exists)) and value.pattern.relationship.type == "GSP_EXPAND" else value)
    elif pattern == "P16":
        projection = query.returns[0]
        expression = projection.expression
        gate = Comparison(Variable(expression.variable), "=", Variable(expression.variable))
        query = replace(query, returns=(replace(projection, expression=replace(expression,
            predicate=_nested_boolean(expression.predicate, gate, complexity, pick, "quantifier_predicate"))),))
    return query


def _add_pattern_reference(case: dict[str, Any]) -> None:
    """Reference only work behind a cheap, visible decisive fact.

    Keep every original edge needed when the cheap predicate is not decisive;
    omit edges only for anchors whose keep value already decides the output.
    This uses the actual keep property, never a synthesized answer label.
    """
    pattern, query = case["pattern"], case["query_a"]
    if pattern in {"P4", "P5", "P15"}:
        keep_value = "true" if pattern == "P4" else "false"
        guard = f"coalesce(u.keep, false) = {keep_value}"
        case["setup"].append(
            "MATCH (u:GspAnchor) WHERE " + guard + " MATCH (u)-[:GSP_EXPAND]->(v:GspLeaf) "
            "CREATE (u)-[:GSP_REFERENCE]->(v)"
        )
        # All branch-relevant edges are retained, including any null-keep
        # anchor (ELSE / non-decisive OR). Both sides use the same leaf nodes.
        case["checks"].append(_count_check(
            "MATCH ()-[r:GSP_REFERENCE]->() RETURN count(r) AS count", 1))
        case["checks"].append(_count_check(
            "MATCH (u:GspAnchor) WHERE " + guard + " MATCH (u)-[:GSP_EXPAND]->(v:GspLeaf) "
            "RETURN count(v) AS count", 1))
        case["checks"].append(_count_check(
            "MATCH (u:GspAnchor)-[:GSP_REFERENCE]->(v:GspLeaf) WHERE "
            "NOT EXISTS { MATCH (u)-[:GSP_EXPAND]->(v) } RETURN count(v) AS count", 0))
        case["query_ref"] = _walk_replace(query, lambda value: replace(value, type="GSP_REFERENCE")
                                           if isinstance(value, Relationship) and value.type == "GSP_EXPAND" else value)
        case["reference_reason"] = "retain edges when keep is non-decisive; omit only work behind the decisive keep value"
    elif pattern == "P16":
        projection = query.returns[0]
        expression = projection.expression
        case["query_ref"] = replace(query, returns=(replace(projection, expression=replace(
            expression, items=Concat(expression.items.left, ListExpr(())))),))
        case["reference_reason"] = "the explicit zero makes ALL false independently of the positive list suffix"
    if "query_ref" in case:
        case["reference_side"] = "ref"
        case["source"]["oracle_mode"] = "decisive_fact_reference"


def instantiate(
    seed_query: SeedQuery | str | None, pattern: str, scale: int, seed: int,
    *, data_seed: int | None = None, complexity: int = 0, state_variant: str = "original",
    composition: dict | None = None,
) -> dict[str, Any]:
    """Populate a finite template; query choices and graph layout have separate seeds.

    Pass None as the input query to use our own templates. A fixed data_seed
    lets different query choices share exactly the same database setup.
    """
    if type(scale) is not int or scale <= 0:
        raise ValueError("scale must be a positive integer")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if data_seed is not None and (type(data_seed) is not int or data_seed < 0):
        raise ValueError("data_seed must be a nonnegative integer or None")
    if type(complexity) is not int or complexity not in (0, 1, 2):
        raise ValueError("complexity must be 0, 1 or 2")
    if not isinstance(state_variant, str) or state_variant not in {"original", "empty_low", "multi_low", "pruning_path"}:
        raise ValueError("state_variant must be original, empty_low, multi_low or pruning_path")
    if state_variant == "empty_low" and pattern not in {"P10", "P11"}:
        raise PatternNotApplicable("empty_low state is only implemented for P10 and P11")
    if state_variant == "multi_low" and pattern not in {"P11", "P13"}:
        raise PatternNotApplicable("multi_low state is only implemented for P11 and P13")
    if state_variant == "pruning_path" and pattern != "P3":
        raise PatternNotApplicable("pruning_path state is only implemented for P3")
    actual_data_seed = seed if data_seed is None else data_seed
    rule = None
    if composition is not None:
        from paper_patterns import compositional_rule
        try:
            if not isinstance(composition, dict):
                raise ValueError("composition requires an options dictionary")
            rule = compositional_rule(pattern, composition.get("form"))
        except ValueError as exc:
            raise PatternNotApplicable(str(exc)) from exc
    skeleton = abstract_seed(seed_query, rule.legacy_pattern if rule else pattern)
    skeleton = replace(skeleton, source={**skeleton.source, "population_seed": seed,
                                         "data_seed": actual_data_seed})
    if composition is not None:
        if state_variant != "original":
            raise PatternNotApplicable("legacy state variants cannot specify a compositional graph")
        if complexity != 0:
            raise PatternNotApplicable("use composition.depth instead of legacy complexity wrapping")
        return _instantiate_composed(skeleton, scale, seed, actual_data_seed, composition, rule)
    if state_variant != "original":
        skeleton = replace(skeleton, source={**skeleton.source, "state_variant": state_variant})
    case = _BUILDERS[pattern](skeleton, scale, actual_data_seed)
    case["seed"], case["data_seed"] = seed, actual_data_seed
    if state_variant != "original":
        case["state_variant"] = state_variant
        case["bindings"]["state_variant"] = state_variant
        if state_variant == "empty_low":
            case["bindings"].update(low_candidates=0, cost_kind="empty_input_pruning", high_matches=1)
        elif state_variant == "multi_low":
            case["bindings"].update(low_candidates=8, low_branch_multiplicity=8,
                                    multiplicity_anchor_uid=1, root_high_candidates=4 * scale + 1,
                                    cost_kind="repeated_branch_work", high_matches=1,
                                    total_low_matches=9 if pattern == "P13" else 8)
            if pattern == "P13":
                case["bindings"]["total_high_matches"] = 2
        else:
            case["bindings"].pop("noise_chains", None)
            case["bindings"].update(cost_kind="path_prefix_pruning", failure_entry_uid=0,
                                    failure_entry_keep=False, failure_fanout=scale, noise_paths=scale,
                                    witness_hops=2, predicate="all(x IN nodes(p) WHERE x.keep = true)")
        case["bindings"]["anchor_candidates"] = 3 if pattern == "P13" else 1
    _populate_variants(case, seed, complexity)
    _add_pattern_reference(case)
    graph = GraphSpec(pattern, scale, actual_data_seed, state_variant)
    case["graph_spec"] = graph.to_dict()
    case["state_checks"] = graph.state_checks()
    case["index_requirements"] = graph.indexes
    validate_query(case["query_a"])
    if "query_ref" in case:
        validate_query(case["query_ref"])
    case["source"]["populated_query_sha256"] = sha256(render_query(case["query_a"]).encode()).hexdigest()
    return case


def _instantiate_composed(skeleton, scale, seed, data_seed, options, rule):
    """Run the paper's symbolic skeleton, construction and instantiation stages."""
    from paper_patterns import PAPER_CATALOG
    from symbolic import build_symbolic_skeleton, construct_state, instantiate_pair

    try:
        symbolic = build_symbolic_skeleton(rule.paper_pattern, options, seed)
        solved = construct_state(symbolic, scale=scale, data_seed=data_seed)
        pair = instantiate_pair(symbolic, solved)
    except ValueError as exc:
        raise PatternNotApplicable(f"symbolic population: {exc}") from exc
    populated = solved.population
    source = replace(skeleton, source={**skeleton.source,
                                      "generation_method": "recursive_symbolic",
                                      "population_inputs": "symbolic_ast_and_joint_constraints",
                                      "seed_usage": "local_rule_seed" if skeleton.source.get("generator") == "builtin_template" else "provenance_only_not_embedded",
                                      "pattern_catalog": PAPER_CATALOG, "paper_pattern": rule.paper_pattern,
                                      "legacy_pattern": rule.legacy_pattern, "pattern_form": rule.form})
    if "builtin_pattern" in source.source:
        source = replace(source, source={**source.source, "builtin_pattern": rule.paper_pattern})
    checks = [_index_check(index["label"], index["property"]) for index in populated.spec.indexes]
    case = _pack(source, scale, seed, pair.query_a, populated.setup,
                 [*checks, *populated.checks, *pair.checks], populated.expected_rows, pair.bindings)
    case.update(pattern=rule.paper_pattern, query_b=pair.query_b, pattern_catalog=PAPER_CATALOG,
                paper_pattern=rule.paper_pattern, legacy_pattern=rule.legacy_pattern, pattern_form=rule.form,
                data_seed=data_seed, composition=json.loads(json.dumps(symbolic.options)),
                skeleton=f"symbolic {rule.paper_pattern}/{rule.form}", placeholders=[],
                symbolic_skeleton=symbolic.to_data(), construction_record=solved.construction_record,
                instantiation_record=pair.instantiation_record, mutation_record=pair.mutation_record,
                graph_spec=populated.spec.to_dict(), state_checks=populated.spec.state_checks(),
                index_requirements=populated.spec.indexes,
                dependency_analysis=solved.construction_record["dependency_analysis"],
                population_constraints=populated.constraints, population_evidence=populated.evidence)
    return case
