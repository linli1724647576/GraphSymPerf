"""M2 contracts: local laws are inserted into a preserved AST-first host."""

from dataclasses import fields, is_dataclass, replace
import json

import pytest

import ir
from ast_pattern.contracts import EmbeddedAst, EmbeddingPort, HostAst, Port, StageFailure, digest
from ast_pattern.embedding import embed_patterns, _symbolic_terminals
from ast_pattern.pattern_registry import DEFAULT_REGISTRY, get_pattern
from dependencies import find_mutation_sites, node_at, replace_at
from symbolic import Hole, Symbol, _assignment, _resolve


def walk(value):
    yield value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk(getattr(value, field.name))
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from walk(child)
    elif isinstance(value, dict):
        for child in value.values():
            yield from walk(child)


def host(kind='rows', expected='rows', *, variable=None, reserve=160):
    port = Port('port:0', kind, expected)
    expression = ir.Count(body=port) if kind == 'rows' else port
    tree = ir.Query(ir.Node(variable, ('HostRoot',)) if variable else None,
                    None, (ir.Alias(expression, 'value'),))
    address = ('returns', 0, 'expression', 'body') if kind == 'rows' else ('returns', 0, 'expression')
    return HostAst(tree, (EmbeddingPort('port:0', address, kind=kind, reserved_budget=reserve),))


def concrete(embedded):
    probe = _symbolic_terminals(embedded.skeleton)
    return _resolve(probe.source, _assignment(probe))


def test_paper_registry_retains_all_laws_and_distinct_p10_forms():
    assert set(DEFAULT_REGISTRY) == {*(f'P{i}' for i in range(1, 14)), *(f'S{i}' for i in range(1, 11))}
    assert len(DEFAULT_REGISTRY) == 23
    assert [get_pattern('P10', form).legacy_pattern for form in ('independent', 'shared_anchor', 'chain')] == ['P10', 'P11', 'P12']
    assert get_pattern('P11').legacy_pattern == 'P13'
    for spec in DEFAULT_REGISTRY.values():
        assert spec.applicability and spec.state_requirements and spec.local_relation
        assert spec.source_interface['imports'] == ()
        assert 'graph_spec' not in spec.to_data()


@pytest.mark.parametrize('pattern', tuple(DEFAULT_REGISTRY))
def test_every_rule_embeds_locally_without_the_historical_host_builder(monkeypatch, pattern):
    import symbolic
    monkeypatch.setattr(symbolic, 'build_symbolic_skeleton', lambda *a, **k: pytest.fail('kernel-first host builder called'))
    original = host()
    before = original.to_data()
    embedded = embed_patterns(original, selection_policy=pattern)
    assert original.to_data() == before
    assert not any(isinstance(item, (Hole, Port)) for item in walk(embedded.tree))
    assert any(isinstance(item, Symbol) for item in walk(embedded.tree))
    assert replace_at(embedded.tree, original.ports[0].address,
                      node_at(original.tree, original.ports[0].address)) == original.tree
    assert embedded.outside_port_digest == digest(original.tree)
    assert embedded.instances[0].site_address[:len(original.ports[0].address)] == original.ports[0].address
    ir.validate_query(concrete(embedded))


@pytest.mark.parametrize('kind,expected,adapter', [
    ('rows', 'rows', 'identity'), ('scalar', 'integer', 'count'),
    ('predicate', 'boolean', 'exists'), ('scalar', 'boolean', 'exists')])
@pytest.mark.parametrize('pattern', ['P3', 'P4', 'S7', 'S8'])
def test_declared_observation_adapters_preserve_the_host_interface(kind, expected, adapter, pattern):
    original = host(kind, expected)
    embedded = embed_patterns(original, selection_policy=pattern)
    assert embedded.origin_map['adapter'] == adapter
    assert replace_at(embedded.tree, original.ports[0].address,
                      node_at(original.tree, original.ports[0].address)) == original.tree
    assert embedded.skeleton.mark.address == embedded.instances[0].site_address
    ir.validate_query(concrete(embedded))


def test_local_names_avoid_capture_even_when_host_uses_the_normal_prefix():
    original = host(variable='pattern_0__u')
    embedded = embed_patterns(original, selection_policy='P3')
    assert embedded.origin_map['local_prefix'] == 'pattern_1__'
    assert embedded.skeleton.anchor.variable == 'pattern_1__u'
    assert embedded.tree.pattern.variable == 'pattern_0__u'
    assert embedded.instances[0].imports == ()


def test_repeated_applicable_shapes_do_not_redirect_the_explicit_target():
    original = host()
    outer = ir.Edge(ir.Node('a', ('HostRoot',)), ir.Relationship(type='HOST'), ir.Node('b'), 'undirected')
    original = replace(original, tree=replace(original.tree, pattern=outer))
    embedded = embed_patterns(original, selection_policy='P1')
    sites = find_mutation_sites(concrete(embedded), 'P1', 'edge_mirror')
    assert len(sites) >= 2
    assert embedded.instances[0].site_address != ('pattern',)
    assert embedded.instances[0].site_address in {site.address for site in sites}


def test_embedding_stage_record_roundtrip_preserves_the_exact_host_and_target():
    original = host()
    embedded = embed_patterns(original, selection_policy='S7', embed_seed=2)
    restored = EmbeddedAst.from_data(json.loads(json.dumps(embedded.to_data())))
    assert restored.to_data() == embedded.to_data()
    assert restored.embedded_digest == embedded.embedded_digest
    assert embed_patterns(original, selection_policy='S7', embed_seed=2).to_data() == embedded.to_data()


def test_unsupported_scalar_interface_is_classified_without_rewriting_host():
    original = host('scalar', 'string')
    before = original.to_data()
    with pytest.raises(StageFailure) as error:
        embed_patterns(original, selection_policy='S7')
    assert error.value.code == 'no_compatible_site'
    assert original.to_data() == before


def test_recorded_port_drift_is_an_explicit_contract_failure():
    original = host()
    original = replace(original, ports=(replace(original.ports[0], address=('returns', 0)),))
    with pytest.raises(StageFailure) as error:
        embed_patterns(original, selection_policy='P3')
    assert error.value.code == 'contract_drift'


def test_pattern_structure_respects_the_reserved_port_budget():
    with pytest.raises(StageFailure) as error:
        embed_patterns(host(reserve=1), selection_policy='S3')
    assert error.value.code == 'embedding_budget_exhausted'


def test_unknown_rule_does_not_fall_back_to_another_pattern():
    with pytest.raises(StageFailure) as error:
        embed_patterns(host(), selection_policy='P999')
    assert error.value.code == 'unsupported_pattern'


def test_local_rule_options_cannot_rebuild_an_outer_context():
    with pytest.raises(StageFailure) as error:
        embed_patterns(host(), selection_policy={'pattern': 'P3', 'options': {'contexts': ['with']}})
    assert error.value.code == 'interface_mismatch'


def test_multiple_registered_ports_are_explicitly_outside_the_initial_contract():
    original = host()
    original = replace(original, ports=(*original.ports, replace(original.ports[0], port_id='port:1')))
    with pytest.raises(StageFailure) as error:
        embed_patterns(original, selection_policy='P3')
    assert error.value.code == 'unsupported_fragment'
