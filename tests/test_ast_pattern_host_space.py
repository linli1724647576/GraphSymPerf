"""Expanded host grammar keeps typed ports, row interfaces and replay intact."""

from dataclasses import fields, is_dataclass
import os

import pytest

import ir
from ast_pattern.contracts import Port, StageFailure, TerminalSlot
from ast_pattern.host_generator import expand_host_structure, generate_random_ast, sample_clause_skeleton


SHAPES = ("node", "edge", "chain", "branch", "converge", "cycle", "disconnected")
ROW_SEEDS = {"union_all": 2, "conditional": 93, "limit": 9}


def shape_case(shape):
    from main import prepare_case
    return prepare_case(None, "P1", 3, 0, 3, ast_options={
        "grammar": {"clauses": ["MATCH", "RETURN"], "match_shapes": [shape],
                    "row_operators": ["query"], "expression_operators": ["leaf", "count", "exists"],
                    "port_kinds": ["rows"], "return_type": "integer"},
        "ast_budget": {"expression_depth": 0}, "max_attempts": 8})


def row_case(kind):
    from ast_pattern.embedding import embed_patterns
    from ast_pattern.state_builder import construct_graph_state
    from ast_pattern.instantiation import instantiate_query
    from ast_pattern.mutation import mutate_target
    from ast_pattern.dependency_check import check_dependencies
    from main import compile_case
    host = generate_random_ast({"clauses": ["RETURN"], "port_kinds": ["scalar"], "return_type": "integer",
                                "row_operators": ["query", kind]}, host_seed=ROW_SEEDS[kind])
    assert any(item["production_id"] == f"rows/{kind}" for item in host.expansion_trace)
    embedded = embed_patterns(host, selection_policy="S7", embed_seed=0)
    state, plan, _ = construct_graph_state(embedded, 3, 3)
    pair = mutate_target(instantiate_query(embedded, state, plan), state, embedded.target_id)
    report = check_dependencies(pair, embedded, state)
    assert report.static["result_relation"] == "proved" and report.static["target_valid"]
    # This fixture exercises generated row shapes through all stage seams;
    # archive/replay is separately checked through the public pipeline.
    return compile_case({"query_ir": {"a": ir.to_data(pair.query_a), "b": ir.to_data(pair.query_b)},
                         "parameters": {}, "graph_spec": state.graph_spec, "setup": state.population.setup,
                         "checks": [*state.population.checks, *pair.checks],
                         "expected_rows": state.population.expected_rows, "reference_side": "a"}, {})


def walk(value):
    yield value
    if isinstance(value, (Port, TerminalSlot)):
        return
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)


def test_distinct_clause_forms_are_real_structures_and_reproducible():
    grammar = {"clauses": ["MATCH", "WITH DISTINCT", "RETURN DISTINCT"]}
    skeleton = sample_clause_skeleton(grammar, seed=3)
    assert [part["kind"] for part in skeleton.clauses] == grammar["clauses"]
    host = generate_random_ast(grammar, host_seed=3)
    assert any(isinstance(clause, ir.With) and clause.distinct for clause in ir.clauses(host.tree))
    assert host.tree.distinct
    assert host.to_data() == generate_random_ast(grammar, host_seed=3).to_data()
    assert host.grammar_version == "typed-host-v2"


def test_separately_supplied_skeleton_obeys_the_expansion_clause_budget():
    skeleton = sample_clause_skeleton({"clauses": ["WITH", "WITH", "RETURN"]})
    with pytest.raises(StageFailure, match="expansion clause budget"):
        expand_host_structure(skeleton, budget={"clauses": 2})


def test_capabilities_can_disable_all_distinct_forms():
    for seed in range(30):
        host = generate_random_ast(capability_profile={"distinct": False}, host_seed=seed)
        assert not any(isinstance(node, (ir.With, ir.Query)) and node.distinct for node in walk(host.tree))
    with pytest.raises(StageFailure):
        generate_random_ast({"clauses": ["RETURN DISTINCT"]}, {"distinct": False})


@pytest.mark.parametrize("restriction", ["grammar", "capabilities"])
def test_distinct_only_return_capability_applies_to_nested_queries(restriction):
    grammar = {"clauses": ["RETURN DISTINCT"]}
    capabilities = None
    if restriction == "grammar":
        grammar["clause_kinds"] = ["RETURN DISTINCT"]
    else:
        capabilities = {"clause_kinds": ["RETURN DISTINCT"]}
    queries = []
    for seed in range(25):
        host = generate_random_ast(grammar, capabilities, host_seed=seed)
        queries.extend(node for node in walk(host.tree) if isinstance(node, ir.Query))
    assert len(queries) > 25
    assert all(node.distinct for node in queries)


def test_multiple_return_columns_have_unique_names_and_one_port():
    hosts = [generate_random_ast({"clauses": ["RETURN"]}, budget={"projections": 3}, host_seed=seed)
             for seed in range(20)]
    assert {len(host.tree.returns) for host in hosts} == {1, 2, 3}
    for host in hosts:
        assert len(set(ir.projection_names(host.tree))) == len(host.tree.returns)
        assert sum(isinstance(node, Port) for node in walk(host.tree)) == 1


@pytest.mark.parametrize("kind,node_type", [("union_all", ir.UnionAll),
                                            ("conditional", ir.ConditionalQuery),
                                            ("limit", ir.RowLimit)])
def test_recursive_row_forms_preserve_branch_interfaces_and_one_port(kind, node_type):
    grammar = {"clauses": ["RETURN"], "port_kinds": ["rows"], "return_type": "integer",
               "row_operators": ["query", kind]}
    seen = False
    for seed in range(80):
        host = generate_random_ast(grammar, host_seed=seed)
        seen |= any(isinstance(node, node_type) for node in walk(host.tree))
        assert sum(isinstance(node, Port) for node in walk(host.tree)) == 1
        for node in walk(host.tree):
            if isinstance(node, ir.RowLimit):
                assert not any(isinstance(child, Port) for child in walk(node))
            if isinstance(node, ir.UnionAll):
                schemas = [tuple(item.name for item in branch.returns) for branch in node.branches
                           if isinstance(branch, ir.Query)]
                assert len(set(schemas)) <= 1
    assert seen


def test_row_forms_respect_capability_restrictions():
    for seed in range(25):
        host = generate_random_ast(capability_profile={"row_operators": ["query"]}, host_seed=seed)
        assert not any(isinstance(node, (ir.UnionAll, ir.ConditionalQuery, ir.RowLimit)) for node in walk(host.tree))


@pytest.mark.parametrize("grammar,budget", [({"match_shapes": ["unknown"]}, None),
                                            ({"row_operators": ["unknown"]}, None),
                                            ({"match_features": ["unknown"]}, None),
                                            (None, {"projections": 0}),
                                            (None, {"expression_arity": 1}),
                                            (None, {"match_edges": 0})])
def test_invalid_search_space_options_are_configuration_errors(grammar, budget):
    with pytest.raises(StageFailure) as failure:
        generate_random_ast(grammar=grammar, budget=budget)
    assert failure.value.code == "invalid_configuration"


@pytest.mark.parametrize("shape", SHAPES)
def test_each_shape_survives_the_six_stage_pipeline_with_equal_complete_bags(shape):
    from executor import typed_bag
    from population import evaluate_query
    from state import GraphSpec
    case = shape_case(shape)
    assert not case.get("compile_error")
    trace = case["stage_records"][0]["data"]["expansion_trace"]
    assert any(part["production_id"] == f"match/{shape}" for part in trace)
    assert case["dependency_report"]["static"]["target_valid"]
    graph = GraphSpec.from_dict(case["graph_spec"])
    for side in ("a", "b"):
        assert typed_bag(evaluate_query(ir.from_data(case["query_ir"][side]), graph)) == typed_bag(case["expected_rows"])


@pytest.mark.parametrize("kind", ROW_SEEDS)
def test_generated_recursive_row_forms_construct_and_preserve_results(kind):
    from executor import typed_bag
    from population import evaluate_query
    from state import GraphSpec
    case = row_case(kind)
    assert not case.get("compile_error")
    graph = GraphSpec.from_dict(case["graph_spec"])
    for side in ("a", "b"):
        assert typed_bag(evaluate_query(ir.from_data(case["query_ir"][side]), graph)) == typed_bag(case["expected_rows"])


@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("GRAPHSYMPERF_TEST_URI"), reason="no disposable Neo4j supplied")
@pytest.mark.parametrize("shape", (*SHAPES, *ROW_SEEDS))
def test_each_expanded_shape_loads_and_preserves_bags_in_neo4j(shape):
    from executor import Neo4j, check_case, measurement_settings
    config = {"dbms": "neo4j", "language": "cypher", "uri": os.environ["GRAPHSYMPERF_TEST_URI"],
              "user": "neo4j", "password_env": "GRAPHSYMPERF_NEO4J_PASSWORD", "database": "neo4j",
              "dedicated": True, "allow_reset": True}
    settings = measurement_settings({"timeout_seconds": 30})
    case = row_case(shape) if shape in ROW_SEEDS else shape_case(shape)
    database = Neo4j(config, settings)
    try:
        database.restore(case)
        check_case(database, case, settings)
    finally:
        database.close()
