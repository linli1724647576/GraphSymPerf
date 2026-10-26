"""Recipe topology, guards and bounded loading, independently of Cypher text."""

import json
from itertools import islice
import random

import pytest

from generator import instantiate
from state import GraphSpec, render_state_checks


TOTALS = {
    "P1": (2, 5, 1, 4), "P2": (2, 2, 2, 2), "P3": (3, 3, 2, 2),
    "P4": (3, 4, 2, 4), "P5": (3, 4, 2, 4), "P6": (2, 2, 1, 1),
    "P7": (2, 2, 1, 1), "P8": (2, 4, 1, 4), "P9": (2, 2, 1, 1),
    "P10": (3, 5, 1, 4), "P11": (3, 4, 2, 4), "P12": (3, 3, 2, 2),
    "P13": (7, 4, 4, 4), "P14": (2, 4, 0, 0), "P15": (4, 4, 2, 4),
    "P16": (0, 0, 0, 0), **{f"S{i}": (4, 4, 3, 4) for i in range(1, 11)},
}


def matches(node, label=None, properties=None):
    return (label is None or label in node["labels"]) and all(
        node["properties"].get(key) == value for key, value in (properties or {}).items())


def check_value(check, nodes, edges):
    """Evaluate only loaded-state facts, not query or recipe expressions."""
    by_uid = {node["uid"]: node for node in nodes}
    kind = check["kind"]
    if kind == "literal":
        return check["value"]
    if kind == "node_count":
        return sum(set(check["labels"]).issubset(node["labels"])
                   and matches(node, properties=check["properties"]) for node in nodes)
    if kind == "node_properties":
        return [{key: node["properties"].get(key) for key in check["properties"]}
                for node in nodes if node["uid"] == check["uid"] and matches(node, check["label"])]
    if kind == "edge_count":
        return sum(edge["type"] == check["type"]
                   and matches(edge, properties=check.get("properties"))
                   and matches(by_uid[edge["source"]], check.get("source_label"), check.get("source_properties"))
                   and matches(by_uid[edge["target"]], check.get("target_label"), check.get("target_properties"))
                   for edge in edges)
    if kind == "distinct_values":
        rows = ([node["properties"] for node in nodes if matches(node, check["label"])]
                if "label" in check else [edge["properties"] for edge in edges if edge["type"] == check["type"]])
        return len({row[check["property"]] for row in rows})
    if kind == "inverse_edges":
        return sum(sum(other["source"] == edge["target"] and other["target"] == edge["source"]
                       and other["properties"][check["pair_key"]] == edge["properties"][check["pair_key"]]
                       for other in edges if other["type"] == check["reverse"]) != 1
                   for edge in edges if edge["type"] == check["forward"])
    if kind == "reference_edges":
        original = {(edge["source"], edge["target"]) for edge in edges if edge["type"] == check["original"]}
        reference = {(edge["source"], edge["target"]) for edge in edges if edge["type"] == check["reference"]}
        required = {(u, v) for u, v in original
                    if (by_uid[u]["properties"].get(check["guard_property"]) or False) == check["guard_value"]}
        return len(reference - original) + len(required - reference)
    raise AssertionError(kind)


@pytest.mark.parametrize("pattern", list(TOTALS))
@pytest.mark.parametrize("scale", [1, 5])
def test_recipe_counts_endpoints_and_facts(pattern, scale):
    spec = GraphSpec(pattern, scale, 17)
    nodes, edges = list(spec.iter_nodes()), list(spec.iter_edges())
    n0, dn, e0, de = TOTALS[pattern]
    assert spec.counts() == {"nodes": n0 + dn * scale, "edges": e0 + de * scale}
    assert len(nodes) == spec.counts()["nodes"]
    assert len(edges) == spec.counts()["edges"]
    by_uid = {node["uid"]: node for node in nodes}
    assert len(by_uid) == len(nodes)
    for edge in edges:
        assert edge["source_label"] in by_uid[edge["source"]]["labels"]
        assert edge["target_label"] in by_uid[edge["target"]]["labels"]
    for check in spec.state_checks():
        expected = [check["properties"]] if check["kind"] == "node_properties" else check["expected"]
        assert check_value(check, nodes, edges) == expected, check
    assert GraphSpec.from_dict(json.loads(json.dumps(spec.to_dict()))) == spec
    assert not any("INDEX" in check["query"] for check in render_state_checks(spec.state_checks()))


@pytest.mark.parametrize("pattern,variant,counts", [
    ("P3", "pruning_path", {"nodes": 14, "edges": 13}),
    ("P10", "empty_low", {"nodes": 28, "edges": 21}),
    ("P11", "empty_low", {"nodes": 24, "edges": 22}),
    ("P11", "multi_low", {"nodes": 30, "edges": 29}),
    ("P13", "multi_low", {"nodes": 34, "edges": 31}),
])
def test_state_variants_change_real_topology(pattern, variant, counts):
    spec = GraphSpec(pattern, 5, 9, variant)
    assert spec.counts() == counts
    nodes, edges = list(spec.iter_nodes()), list(spec.iter_edges())
    for check in spec.state_checks():
        expected = [check["properties"]] if check["kind"] == "node_properties" else check["expected"]
        assert check_value(check, nodes, edges) == expected
    if variant == "pruning_path":
        assert next(node for node in nodes if node["uid"] == 0)["properties"]["keep"] is False
        assert sum(edge["source"] == 0 for edge in edges) == 5
    if variant == "empty_low":
        if pattern == "P10":
            assert next(node for node in nodes if "GspLow" in node["labels"])["properties"]["id"] == 0
        else:
            assert not any(edge["type"] == "GSP_LOW" and edge["source"] == 1 for edge in edges)
            assert any(edge["type"] == "GSP_LOW" for edge in edges)


@pytest.mark.parametrize("pattern", ["P4", "P5", "P15"])
def test_reference_retains_exact_non_decisive_branch_and_null_guard(pattern):
    spec = GraphSpec(pattern, 3, 0)
    nodes, edges = list(spec.iter_nodes()), list(spec.iter_edges())
    ref = [edge for edge in edges if edge["type"] == "GSP_REFERENCE"]
    assert [(edge["source"], edge["target"]) for edge in ref] == [(2, 4 if pattern == "P15" else 3)]
    check = next(check for check in spec.state_checks() if check["kind"] == "reference_edges")
    assert check_value(check, nodes, edges) == 0
    assert check_value(check, nodes, [edge for edge in edges if edge not in ref]) == 1
    if pattern == "P15":
        null = next(check for check in spec.state_checks() if check["kind"] == "node_properties" and check["uid"] == 3)
        assert null["properties"]["keep"] is None
    no_reference = GraphSpec(pattern, 3, 0, include_reference=False)
    assert no_reference.counts()["edges"] == spec.counts()["edges"] - 1


def test_inverse_checks_detect_wrong_direction_and_duplicate_inverse():
    spec = GraphSpec("P2", 3, 0)
    nodes, edges = list(spec.iter_nodes()), list(spec.iter_edges())
    check = next(check for check in spec.state_checks() if check["kind"] == "inverse_edges")
    inverse = next(edge for edge in edges if edge["type"] == "GSP_REVERSE")
    assert check_value(check, nodes, edges) == 0
    reversed_inverse = {**inverse, "source": inverse["target"], "target": inverse["source"]}
    assert check_value(check, nodes, [reversed_inverse if edge is inverse else edge for edge in edges]) == 1
    assert check_value(check, nodes, [*edges, inverse]) == 1


def test_schema_retains_exact_relationship_endpoints_and_properties():
    schema = GraphSpec("P3", 1, 0, "pruning_path").schema()
    relation = schema["relationships"][0]
    assert {(pair["source_label"], pair["target_label"]) for pair in relation["endpoints"]} == {
        ("GspLow", "GspMiddle"), ("GspMiddle", "GspHigh"),
        ("GspLow", "GspBad"), ("GspBad", "GspMiddle"),
    }
    assert GraphSpec("P2", 1, 0).schema()["relationships"][0]["properties"] == {"pair": "integer"}
    assert next(node for node in GraphSpec("P15", 1, 0).schema()["nodes"] if node["label"] == "GspAnchor")["properties"]["keep"] == "boolean"


@pytest.mark.parametrize("pattern", [f"S{i}" for i in range(1, 11)])
def test_scor_recipe_matches_literal_legacy_construction(pattern):
    """Check exact identities against the original builder, not recipe counts.

    The reference population in generator._scor_state is range(2, 4),
    inclusive. In particular, uid=1 remains the root, not a reference target.
    """
    scale, data_seed = 3, 0
    case = instantiate(None, pattern, scale, 0, data_seed=data_seed, complexity=1)
    assert case["setup"][-1] == (
        "MATCH (u:ScorRoot {id: 1}) UNWIND range(2, 4) AS i "
        "CREATE (u)-[:GSP_SC_REF]->(:ScorTarget {uid: i, mark: true, key: 1})"
    )
    offset = random.Random(data_seed).randrange(10_000, 1_000_000)
    assert case["setup"][-2] == (
        "MATCH (u:ScorRoot {id: 1}) UNWIND range(1, 12) AS i "
        f"CREATE (u)-[:GSP_SC_HIGH]->(:ScorTarget {{uid: {offset}+i, mark: true, key: 1}})"
    )
    spec = GraphSpec.from_dict(case["graph_spec"])
    expected = [{"uid": 1, "labels": ["ScorRoot"],
                 "properties": {"id": 1, "uid": 1, "key": 1, "expected_count": 12}}]
    expected += [{"uid": uid, "labels": ["ScorTarget"],
                  "properties": {"uid": uid, "mark": True, "key": 1}}
                 for uid in [*range(offset + 1, offset + 13), 2, 3, 4]]
    assert list(spec.iter_nodes()) == expected
    edges = list(spec.iter_edges())
    assert [(edge["source"], edge["target"]) for edge in edges if edge["type"] == "GSP_SC_REF"] == [(1, 2), (1, 3), (1, 4)]
    assert [(edge["source"], edge["target"]) for edge in edges if edge["type"] == "GSP_SC_HIGH"] == [
        (1, uid) for uid in range(offset + 1, offset + 13)]
    assert all(edge["source_label"] == "ScorRoot" and edge["target_label"] == "ScorTarget" and edge["properties"] == {}
               for edge in edges)


def test_large_recipe_and_batches_remain_bounded():
    spec = GraphSpec("P1", 10**9, 0)
    assert len(json.dumps(spec.to_dict())) < 200
    batches = spec.iter_batches("nodes", 7)
    first = list(islice(batches, 3))
    assert [len(batch) for batch in first] == [7, 7, 7]
    assert len({row["uid"] for batch in first for row in batch}) == 21
    assert spec.counts() == {"nodes": 5_000_000_002, "edges": 4_000_000_001}
    # No eager node or edge lists are needed to compute schema/check metadata.
    assert spec.schema()["nodes"]
    assert spec.state_checks()


@pytest.mark.parametrize("pattern", list(TOTALS))
def test_generator_keeps_recipe_independent_of_query_population(pattern):
    cases = [instantiate(None, pattern, 2, seed, data_seed=8, complexity=2) for seed in (0, 17)]
    assert cases[0]["graph_spec"] == cases[1]["graph_spec"]
    assert cases[0]["state_checks"] == cases[1]["state_checks"]
    assert cases[0]["index_requirements"] == cases[1]["index_requirements"]
    assert cases[0]["setup"] == cases[1]["setup"]
    assert all(check["kind"] != "index" for check in cases[0]["state_checks"])


@pytest.mark.parametrize("kwargs", [{"scale": 0}, {"data_seed": True}, {"version": 2},
                                    {"state_variant": "pruning_path"}, {"include_reference": 1}])
def test_recipe_rejects_invalid_or_inapplicable_input(kwargs):
    params = {"pattern": "P1", "scale": 1, "data_seed": 0, **kwargs}
    with pytest.raises(ValueError):
        GraphSpec(**params)


def test_null_population_check_observes_both_null_and_missing_properties():
    spec = GraphSpec('AST', 1, 0, version=2, node_groups=(
        {'count': 1, 'labels': ['Item'], 'properties': {'uid': 1, 'flag': None}},
        {'count': 1, 'labels': ['Item'], 'properties': {'uid': 2}},
        {'count': 1, 'labels': ['Item'], 'properties': {'uid': 3, 'flag': False}},
    ))
    check = {'kind': 'node_count', 'labels': ['Item'], 'properties': {'flag': None}, 'expected': 2}
    assert check_value(check, list(spec.iter_nodes()), []) == 2
    rendered, = render_state_checks([check])
    assert rendered == {'query': 'MATCH (n:Item) WHERE n.flag IS NULL RETURN count(n) AS `count`',
                        'expected_rows': [{'count': 2}]}
    generated = render_state_checks(spec.state_checks())
    assert any('n.flag IS NULL' in item['query'] for item in generated)
    assert all('flag: null' not in item['query'] for item in generated)


def test_null_relationship_and_endpoint_checks_preserve_direction_and_nonnull_values():
    nodes = [{'uid': 1, 'labels': ['Left'], 'properties': {'uid': 1, 'flag': None}},
             {'uid': 2, 'labels': ['Right'], 'properties': {'uid': 2}}]
    edges = [{'source': 1, 'target': 2, 'type': 'R', 'properties': {'score': 0}},
             {'source': 2, 'target': 1, 'type': 'R', 'properties': {'score': 0}},
             {'source': 1, 'target': 2, 'type': 'R', 'properties': {'score': 1}}]
    check = {'kind': 'edge_count', 'type': 'R', 'source_label': 'Left', 'target_label': 'Right',
             'source_properties': {'uid': 1, 'flag': None}, 'target_properties': {'uid': 2, 'flag': None},
             'properties': {'score': 0, 'missing': None}, 'expected': 1}
    assert check_value(check, nodes, edges) == 1
    rendered, = render_state_checks([check])
    assert rendered['query'] == ('MATCH (u:Left {uid: 1})-[r:R {score: 0}]->(v:Right {uid: 2}) '
                                 'WHERE u.flag IS NULL AND r.missing IS NULL AND v.flag IS NULL '
                                 'RETURN count(r) AS `count`')
    assert rendered['expected_rows'] == [{'count': 1}]


def test_null_check_does_not_drop_false_or_zero_properties():
    check = {'kind': 'node_count', 'labels': ['Item'],
             'properties': {'flag': False, 'rank': 0, 'missing': None}, 'expected': 1}
    rendered, = render_state_checks([check])
    assert rendered['query'] == ('MATCH (n:Item {flag: false, rank: 0}) WHERE n.missing IS NULL '
                                 'RETURN count(n) AS `count`')


def test_degree_checks_aggregate_all_groups_that_reach_the_same_endpoint_uid():
    def affine(base, step=1):
        return {'affine': {'base': base, 'step': step}}

    def edge(count, source, target):
        return {'count': count, 'source': source, 'target': target,
                'source_label': 'Source', 'target_label': 'Target', 'type': 'R'}

    spec = GraphSpec('AST', 7, 0, version=2, node_groups=(
        {'count': 7, 'labels': ['Source'], 'properties': {'uid': affine(1)}},
        {'count': 7, 'labels': ['Target'], 'properties': {'uid': affine(9)}},
    ), edge_groups=(
        edge(1, 1, 9),
        edge(6, affine(1), 9),
        edge(6, 1, affine(9)),
        edge(3, affine(2, 2), affine(9)),
    ))
    nodes, edges = list(spec.iter_nodes()), list(spec.iter_edges())
    checks = spec.state_checks()
    for check in checks:
        expected = [check['properties']] if check['kind'] == 'node_properties' else check['expected']
        assert check_value(check, nodes, edges) == expected, check
    degree_checks = {(tuple(check['source_properties'].items()), tuple(check['target_properties'].items())):
                     check['expected'] for check in checks if check['kind'] == 'edge_count'
                     and 'source_properties' in check}
    assert degree_checks[((), (('uid', 9),))] == 9
    assert degree_checks[((('uid', 1),), ())] == 8
    assert degree_checks[((('uid', 1),), (('uid', 9),))] == 3


@pytest.mark.parametrize('source_base,target_base,step,contribution', [
    (1, 9, 1, 1), (1, 10, 1, 0), (3, 11, -1, 1), (1, 9, 0, 3),
])
def test_pair_checks_intersect_affine_source_and_target_indices(source_base, target_base, step, contribution):
    def affine(base):
        return {'affine': {'base': base, 'step': step}}

    spec = GraphSpec('AST', 3, 0, version=2, node_groups=tuple(
        {'count': 1, 'labels': [label], 'properties': {'uid': uid}}
        for label, values in [('Source', range(1, 4)), ('Target', range(9, 13))] for uid in values
    ), edge_groups=(
        {'count': 1, 'source': 1, 'target': 9, 'source_label': 'Source', 'target_label': 'Target', 'type': 'R'},
        {'count': 3, 'source': affine(source_base), 'target': affine(target_base),
         'source_label': 'Source', 'target_label': 'Target', 'type': 'R'},
    ))
    check = next(check for check in spec.state_checks() if check['kind'] == 'edge_count'
                 and check.get('source_properties') == {'uid': 1} and check.get('target_properties') == {'uid': 9})
    assert check['expected'] == 1 + contribution
    assert check_value(check, list(spec.iter_nodes()), list(spec.iter_edges())) == check['expected']
