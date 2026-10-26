import json
from dataclasses import replace

import pytest

from dependencies import analyze_dependencies, find_p3_sites, node_at, propagate_equivalence, replace_at
from ir import (
    Alias, All, Boolean, Call, Comparison, Count, Edge, Exists, Literal, Match, Node,
    Property, Query, QueryError, Relationship, Variable, With, from_data, to_data,
)
from mutator import find_patterns, mutate


def path_query(*, variable='p', left='u', right='v'):
    edge = Edge(Node(left, ('A',), (('id', Literal(1)),)),
                Relationship(type='R', min_hops=1, max_hops=3), Node(right, ('B',)),
                'undirected', variable)
    predicate = All('x', Call('nodes', (Variable(variable),)),
                    Comparison(Property('x', 'keep'), '=', Literal(True)))
    return Query(edge, predicate, (Property(left, 'uid'), Property(right, 'uid'),
                                   Alias(Call('length', (Variable(variable),)), 'hops')))


def test_structural_addresses_and_analysis_survive_serialization():
    query = Query(None, None, (Alias(Count(body=path_query()), 'total'),))
    cloned = from_data(json.loads(json.dumps(to_data(query))))
    assert analyze_dependencies(query).to_dict() == analyze_dependencies(cloned).to_dict()
    site, = find_p3_sites(query)
    assert site.address == ('returns', 0, 'expression', 'body', 'pattern')
    assert site.path == site.address
    assert node_at(query, site.address) == path_query().pattern
    assert json.loads(json.dumps(site.to_dict()))['guards']['reversal_invariant'] is True
    assert json.loads(json.dumps(analyze_dependencies(query).to_dict()))['scopes']


def test_p3_rewrites_the_selected_nested_site_only():
    query = Query(None, None, (Alias(Count(body=path_query()), 'first'),
                               Alias(Count(body=path_query()), 'second')))
    sites = find_p3_sites(query)
    assert len(sites) == 2
    changed = mutate(query, 'P3', bindings={'mutation_site': list(sites[1].address)})
    assert changed.returns[0] == query.returns[0]
    edge = node_at(changed, sites[1].address)
    assert edge.left == path_query().pattern.right
    assert edge.right == path_query().pattern.left
    assert mutate(changed, 'P3', bindings={'mutation_site': sites[1].address}) == query
    assert mutate(query, 'P3').returns[1] == query.returns[1]
    assert 'P3' in find_patterns(query)


def test_nested_exists_and_count_have_actual_correlated_imports():
    inner = path_query(left='anchor')
    counted = Query(Node('anchor'), None, (Alias(Count(body=inner), 'n'),))
    query = Query(Node('anchor'), None, (Alias(Exists(body=counted), 'ok'),))
    analysis = analyze_dependencies(query)
    anchor = next(symbol for symbol in analysis.symbols
                  if symbol['name'] == 'anchor' and symbol['kind'] == 'node')
    nested = [scope for scope in analysis.scopes if scope['kind'] == 'query' and scope['parent_scope']]
    assert len(nested) == 2
    assert all(anchor['symbol_id'] in scope['imports'] for scope in nested)
    site, = find_p3_sites(query)
    assert site.endpoint_symbols['left'] == anchor['symbol_id']
    assert 'body' in site.address
    assert mutate(query, 'P3') != query


def test_transitive_correlation_is_imported_through_scalar_subquery_and_with():
    scalar = Count(Node('z'), Comparison(
        Count(body=Query(Node('u'), None, (Literal(1),))), '>', Literal(0)))
    query = Query(None, None, (Variable('n'),), pipeline=(
        Match(Node('u')), With((Alias(scalar, 'n'),)),
    ))
    analysis = analyze_dependencies(query)
    u = next(symbol for symbol in analysis.symbols if symbol['kind'] == 'node' and symbol['name'] == 'u')
    count_scope = next(scope for scope in analysis.scopes if scope['kind'] == 'count')
    with_scope = next(scope for scope in analysis.scopes if scope['kind'] == 'with')
    assert count_scope['imports'] == [u['symbol_id']]
    assert with_scope['imports'] == [u['symbol_id']]
    n = next(symbol for symbol in analysis.symbols
             if symbol['name'] == 'n' and symbol['definition'].endswith('projections/0'))
    assert n['sources'] == [u['symbol_id']]


def test_with_alias_records_expression_sources_and_consumers():
    base = path_query()
    query = Query(None, None, (Variable('hops'),), pipeline=(
        Match(base.pattern, base.where),
        With((Alias(Call('length', (Variable('p'),)), 'hops'),)),
    ))
    analysis = analyze_dependencies(query)
    path = next(symbol for symbol in analysis.symbols if symbol['kind'] == 'path')
    alias = next(symbol for symbol in analysis.symbols if symbol['name'] == 'hops'
                 and symbol['definition'] == 'root/pipeline/1/projections/0')
    assert alias['sources'] == [path['symbol_id']]
    assert any(use['symbol_id'] == alias['symbol_id'] and use['node_id'] == 'root/returns/0'
               for use in analysis.uses)
    site, = find_p3_sites(query)
    assert site.address == ('pipeline', 0, 'pattern')
    assert mutate(query, 'P3').pipeline[1] == query.pipeline[1]


def test_with_passthrough_keeps_entity_identity_and_stops_dropped_bindings():
    base = path_query()
    query = Query(None, None, base.returns, pipeline=(Match(Node('u')),
                  With((Variable('u'),)), Match(base.pattern, base.where)))
    analysis = analyze_dependencies(query)
    definitions = [symbol for symbol in analysis.symbols if symbol['name'] == 'u'
                   and symbol['kind'] == 'node']
    assert len(definitions) == 1
    site, = find_p3_sites(query)
    assert site.endpoint_symbols['left'] == definitions[0]['symbol_id']
    assert site.address == ('pipeline', 2, 'pattern')
    dropped = replace(query, pipeline=(query.pipeline[0], With((Alias(Literal(1), 'n'),))),
                      returns=(Property('u', 'uid'),))
    with pytest.raises(QueryError, match='Unbound'):
        analyze_dependencies(dropped)


def test_path_passthrough_and_scalar_alias_with_same_spelling_are_distinguished():
    base = path_query()
    query = Query(None, None, (Variable('p'),), pipeline=(
        Match(base.pattern, base.where),
        With((Variable('p'),)),
        With((Alias(Call('length', (Variable('p'),)), 'p'),)),
    ))
    analysis = analyze_dependencies(query)
    original_path = next(symbol for symbol in analysis.symbols if symbol['kind'] == 'path')
    renamed_scalar = next(symbol for symbol in analysis.symbols
                          if symbol['name'] == 'p' and symbol['kind'] == 'scalar'
                          and symbol['definition'].endswith('projections/0'))
    assert original_path['symbol_id'] != renamed_scalar['symbol_id']
    final_use = next(use for use in analysis.uses if use['node_id'] == 'root/returns/0')
    assert final_use['symbol_id'] == renamed_scalar['symbol_id']
    site, = find_p3_sites(query)
    assert {use['observation'] for use in site.guards['path_consumers']} == {
        'all_nodes_local', 'passthrough', 'length'}
    assert mutate(query, 'P3').pipeline[1:] == query.pipeline[1:]


def test_all_cannot_shadow_a_currently_visible_outer_binding():
    query = Query(Node('x'), None, (Alias(Count(body=path_query()), 'n'),))
    with pytest.raises(QueryError, match='shadow'):
        analyze_dependencies(query)


def test_all_binder_with_same_name_as_a_dropped_outer_node_has_distinct_symbol():
    inner = path_query()
    query = Query(None, None, (Alias(Count(body=inner), 'n'), Variable('z')),
                  pipeline=(Match(Node('x')), With((Alias(Literal(1), 'z'),))))
    analysis = analyze_dependencies(query)
    x_symbols = [symbol for symbol in analysis.symbols if symbol['name'] == 'x']
    assert len(x_symbols) == 2
    assert len({symbol['symbol_id'] for symbol in x_symbols}) == 2
    binder = next(symbol for symbol in x_symbols if symbol['scope_id'].endswith(':all'))
    binder_uses = [use for use in analysis.uses if use['symbol_id'] == binder['symbol_id']]
    assert len(binder_uses) == 1
    assert 'predicate' in binder_uses[0]['address']
    assert len(find_p3_sites(query)) == 1


def test_equivalence_propagates_bags_through_count_alias_and_filter_context():
    query = Query(None, None, (Variable('n'),), pipeline=(
        With((Alias(Count(body=path_query()), 'n'),),
             Comparison(Variable('n'), '>', Literal(0))),
    ))
    site, = find_p3_sites(query)
    evidence = propagate_equivalence(query, site)
    operators = [step['operator'] for step in evidence]
    assert operators == ['Edge', 'Query', 'Count', 'Alias', 'With', 'Query']
    count = next(step for step in evidence if step['operator'] == 'Count')
    assert count['premise'] == 'equal_input_bags'
    assert count['relation'] == 'integer_equal'
    assert evidence[-1]['relation'] == 'bag_equal'
    assert evidence[-1]['stages'][0]['aliases']
    assert json.loads(json.dumps(evidence)) == evidence


def test_equivalence_propagates_through_correlated_exists():
    query = Query(Node('u'), None, (Alias(Exists(body=path_query()), 'ok'),))
    site, = find_p3_sites(query)
    evidence = propagate_equivalence(query, site)
    exists = next(step for step in evidence if step['operator'] == 'Exists')
    assert exists['premise'] == 'equal_input_bags'
    assert exists['relation'] == 'boolean_equal'
    inner = [step for step in evidence if step['operator'] == 'Query'][0]
    assert inner['imports'] == [site.endpoint_symbols['left']]


def test_propagation_rejects_a_site_not_present_in_this_tree():
    site, = find_p3_sites(path_query())
    with pytest.raises(QueryError, match='applicable P3'):
        propagate_equivalence(Query(None, None, (Literal(1),)), site)


def test_same_path_spelling_in_separate_subqueries_is_not_global_identity():
    query = Query(None, None, (Alias(Count(body=path_query()), 'a'),
                               Alias(Count(body=path_query()), 'b')))
    sites = find_p3_sites(query)
    assert sites[0].path_symbol != sites[1].path_symbol
    assert all(len(site.guards['path_consumers']) == 2 for site in sites)


@pytest.mark.parametrize('address', [('missing',), ('returns', 99), ('returns', -1),
                                    ('returns', True), ('returns', 0, '__class__')])
def test_invalid_structural_addresses_do_not_resolve_or_replace(address):
    with pytest.raises(QueryError, match='structural address'):
        node_at(path_query(), address)
    with pytest.raises(QueryError, match='structural address'):
        replace_at(path_query(), address, Literal(0))


@pytest.mark.parametrize('requested', [('returns',), ['pattern', 'left'], 'pattern', [True]])
def test_wrong_mutation_site_is_rejected(requested):
    with pytest.raises(QueryError, match='mutation_site'):
        mutate(path_query(), 'P3', bindings={'mutation_site': requested})


def test_unbound_path_and_order_sensitive_observations_are_rejected():
    base = path_query()
    invalid = (
        replace(base, returns=(Call('length', (Variable('missing'),)),)),
        replace(base, returns=(Variable('p'),)),
        replace(base, returns=(Call('nodes', (Variable('p'),)),)),
    )
    for query in invalid:
        with pytest.raises(QueryError):
            find_p3_sites(query)


def test_unbounded_path_and_ancestor_limit_are_not_p3_sites():
    base = path_query()
    unbounded = replace(base, pattern=replace(base.pattern, path_variable=None,
        relationship=replace(base.pattern.relationship, min_hops=None, max_hops=None)),
        where=None, returns=(Property('u', 'uid'),))
    assert not find_p3_sites(unbounded)
    outer = Query(None, None, (Alias(Count(body=base), 'n'),), limit=1)
    assert not find_p3_sites(outer)
    assert 'P3' not in find_patterns(outer)
    with pytest.raises(QueryError):
        mutate(outer, 'P3')
    inner_limit = Query(None, None, (Alias(Count(body=replace(base, limit=1)), 'n'),))
    assert not find_p3_sites(inner_limit)


def shared_branches_query(*, cross_dependency=False):
    left = Match(Edge(Node('u'), Relationship(type='LEFT'), Node('a')),
                 Comparison(Property('a', 'keep'), '=', Literal(True)))
    right = Match(Edge(Node('u'), Relationship(type='RIGHT'), Node('b')),
                  Comparison(Property('a' if cross_dependency else 'b', 'keep'), '=', Literal(True)))
    return Query(Node('u', ('Anchor',)), None,
                 (Property('u', 'uid'), Property('a', 'uid'), Property('b', 'uid')),
                 (left, right))


def paper_mutate(query, site):
    return mutate(query, site.legacy_pattern, bindings={
        'paper_pattern': site.paper_pattern, 'form': site.form,
        'legacy_pattern': site.legacy_pattern, 'mutation_site': list(site.address),
    })


def test_paper_and_sites_are_recursive_and_stable_without_reinterpreting_legacy():
    from dependencies import find_mutation_sites
    predicate = Boolean('AND', (Literal(True), Literal(None)))
    inner = Query(Node('u'), predicate, (Property('u', 'uid'),))
    query = Query(Node('u'), None, (Alias(Count(body=inner), 'n'),))
    site, = find_mutation_sites(query, 'P4')
    assert site.address == ('returns', 0, 'expression', 'body', 'where')
    changed = paper_mutate(query, site)
    assert changed.returns[0].expression.body.where.terms == tuple(reversed(predicate.terms))
    assert paper_mutate(changed, find_mutation_sites(changed, 'P4')[0]) == query
    cloned = from_data(json.loads(json.dumps(to_data(query))))
    assert [item.to_dict() for item in find_mutation_sites(cloned, 'P4')] == [site.to_dict()]
    evidence = propagate_equivalence(query, site)
    assert evidence[0]['relation'] == 'boolean_equal'
    assert evidence[-1]['relation'] == 'bag_equal'
    assert next(step for step in evidence if step['operator'] == 'Count')['relation'] == 'integer_equal'


@pytest.mark.parametrize('left', [True, False, None])
@pytest.mark.parametrize('right', [True, False, None])
def test_and_swap_preserves_each_three_valued_input(left, right):
    from dependencies import find_mutation_sites
    # A third distinct term keeps equal-input cases structurally mutable.
    expression = Boolean('AND', (Literal(left), Literal(right), Comparison(Literal(1), '=', Literal(1))))
    query = Query(None, None, (Alias(expression, 'value'),))
    site, = find_mutation_sites(query, 'P4')
    changed = paper_mutate(query, site)
    def truth(terms):
        values = [term.value if isinstance(term, Literal) else True for term in terms]
        return False if False in values else None if None in values else True
    assert truth(query.returns[0].expression.terms) is truth(changed.returns[0].expression.terms)


def test_paper_shared_anchor_numbering_is_explicit_and_optional_is_not_reassigned():
    from dependencies import find_mutation_sites
    query = shared_branches_query()
    site, = find_mutation_sites(query, 'P10', 'shared_anchor')
    assert (site.paper_pattern, site.form, site.legacy_pattern) == ('P10', 'shared_anchor', 'P11')
    assert find_mutation_sites(query, 'P11') == ()
    assert find_mutation_sites(query, 'P10', 'optional') == ()
    changed = paper_mutate(query, site)
    assert changed.matches == tuple(reversed(query.matches))
    assert mutate(query, 'P11') == changed  # Historical code numbering is unchanged.
    with pytest.raises(QueryError):
        mutate(query, 'P10')
    with pytest.raises(QueryError, match='legacy'):
        mutate(query, 'P10', bindings={'paper_pattern': 'P10', 'form': 'shared_anchor'})
    evidence = propagate_equivalence(query, site)
    assert evidence[0]['operator'] == 'MatchPair'
    assert evidence[-1]['relation'] == 'bag_equal'


def test_shared_anchor_rejects_cross_branch_predicate_and_binding_dependencies():
    from dependencies import find_mutation_sites
    assert find_mutation_sites(shared_branches_query(cross_dependency=True), 'P10', 'shared_anchor') == ()
    query = shared_branches_query()
    second = replace(query.matches[1], pattern=replace(query.matches[1].pattern, right=Node('a')),
                     where=Comparison(Property('a', 'keep'), '=', Literal(True)))
    assert find_mutation_sites(replace(query, matches=(query.matches[0], second), returns=query.returns[:2]),
                               'P10', 'shared_anchor') == ()
    assert find_mutation_sites(replace(query, matches=(query.matches[0], replace(query.matches[1], optional=True))),
                               'P10', 'shared_anchor') == ()


def test_branch_locality_follows_nested_symbols_not_same_spelling():
    from dependencies import find_mutation_sites
    base = shared_branches_query()
    correlated = Count(body=Query(Node('a'), None, (Literal(1),)))
    right = replace(base.matches[1], where=Comparison(correlated, '>', Literal(0)))
    assert find_mutation_sites(replace(base, matches=(base.matches[0], right)), 'P10', 'shared_anchor') == ()
    shadowed = Count(body=Query(None, None, (Property('a', 'uid'),), pipeline=(
        With((Alias(Literal(1), 'n'),)), Match(Node('a', ('Local',))),
    )))
    right = replace(base.matches[1], where=Comparison(shadowed, '>', Literal(0)))
    site, = find_mutation_sites(replace(base, matches=(base.matches[0], right)), 'P10', 'shared_anchor')
    assert site.guards['predicates_branch_local']


def test_explicit_catalog_forms_and_unordered_limit_guard():
    from dependencies import find_mutation_sites
    assert find_mutation_sites(path_query(), 'P3', 'bounded_path')[0].form == 'bounded_path'
    query = Query(None, None, (Alias(Boolean('AND', (Literal(True), Literal(False))), 'value'),))
    assert find_mutation_sites(query, 'P4', 'and')[0].form == 'and'
    assert find_mutation_sites(replace(query, limit=1), 'P4', 'and') == ()
    assert find_mutation_sites(replace(shared_branches_query(), limit=1), 'P10', 'shared_anchor') == ()


def test_shared_anchor_resolves_with_shadowing_and_nested_body_symbols():
    from dependencies import find_mutation_sites
    base = shared_branches_query()
    query = Query(None, None, base.returns, pipeline=(
        Match(Node('u', ('Old',))), With((Alias(Literal(1), 'n'),)),
        Match(Node('u', ('Anchor',))), *base.matches,
    ))
    site, = find_mutation_sites(query, 'P10', 'shared_anchor')
    analysis = analyze_dependencies(query)
    anchors = [symbol for symbol in analysis.symbols if symbol['name'] == 'u' and symbol['kind'] == 'node']
    assert len(anchors) == 2
    assert site.guards['anchor_symbol'] == anchors[1]['symbol_id']
    assert site.address == ('pipeline', 3)
    nested = Query(None, None, (Alias(Count(body=query), 'n'),))
    nested_site, = find_mutation_sites(nested, 'P10', 'shared_anchor')
    assert nested_site.address == ('returns', 0, 'expression', 'body', 'pipeline', 3)
    assert paper_mutate(nested, nested_site).returns[0].expression.body.pipeline[3:] == tuple(reversed(query.pipeline[3:]))
    boundary = replace(query, pipeline=(*query.pipeline[:4], With((Variable('u'), Variable('a'))), query.pipeline[4]))
    assert find_mutation_sites(boundary, 'P10', 'shared_anchor') == ()


@pytest.mark.parametrize('left_targets,right_targets', [([], []), ([], [2]), ([1], []), ([1, 1], [2, 3]), ([1, 4], [2, 2])])
def test_shared_anchor_swap_preserves_bag_for_every_imported_anchor(left_targets, right_targets):
    from collections import Counter
    from dependencies import find_mutation_sites
    query = shared_branches_query()
    site, = find_mutation_sites(query, 'P10', 'shared_anchor')
    changed = paper_mutate(query, site)
    def bag(tree, anchor):
        rows = [{'u': anchor}, {'u': anchor}]
        for clause in tree.matches:
            edge = clause.pattern
            targets = left_targets if edge.relationship.type == 'LEFT' else right_targets
            rows = [row | {edge.right.variable: target} for row in rows for target in targets]
        return Counter((row['u'], row['a'], row['b']) for row in rows)
    for anchor in (7, 13):
        assert bag(query, anchor) == bag(changed, anchor)


def stream_branches():
    witness = Query(Node('w', ('Witness',)), None, (Alias(Literal(1), 'value'),))
    low = Query(Node('l', ('Small',)), None, (Alias(Literal(1), 'value'),))
    high = Query(Node('h', ('Large',)), None, (Alias(Literal(1), 'value'),))
    return witness, low, high


def test_recursive_union_outputs_are_connected_to_the_scalar_consumer():
    from ir import UnionAll
    witness, low, _ = stream_branches()
    query = Query(None, None, (Alias(Count(body=UnionAll((witness, low))), 'n'),))
    analysis = analyze_dependencies(query).to_dict()
    union = next(scope for scope in analysis['scopes'] if scope['kind'] == 'union_all')
    output = next(symbol for symbol in analysis['symbols'] if symbol['symbol_id'] == union['exports']['value'])
    assert len(output['sources']) == 2
    assert len(analysis['row_uses']) >= 3
    assert any(use['consumer'] == 'Count' and use['producer'] == union['scope_id']
               for use in analysis['row_uses'])


def test_paper_s6_discovers_the_union_tail_inside_the_limit_envelope():
    from dependencies import find_mutation_sites
    from ir import RowLimit, UnionAll
    witness, low, _ = stream_branches()
    query = RowLimit(UnionAll((witness, low)), 2)
    site, = find_mutation_sites(query, 'S6', 'bounded_demand')
    assert site.address == ('body', 'branches', 1)
    assert site.guards['witness_site'] == ['body', 'branches', 0]
    assert site.guards['envelope_site'] == []
    assert site.guards['required_rows'] == 2


def test_paper_s8_discovers_the_union_tail_inside_an_exists_expression():
    from dependencies import find_mutation_sites
    from ir import UnionAll
    witness, low, _ = stream_branches()
    # An explicit outer binding keeps the existing correlated EXISTS contract.
    witness = replace(witness, pattern=Node('u', ('Witness',)))
    query = Query(Node('u', ('Witness',)), None,
                  (Alias(Exists(body=UnionAll((witness, low))), 'exists'),))
    site, = find_mutation_sites(query, 'S8', 'existential_witness')
    assert site.address == ('returns', 0, 'expression', 'body', 'branches', 1)
    assert site.guards['required_rows'] == 1
    assert site.guards['envelope_site'] == ['returns', 0, 'expression']


def test_s6_rejects_branch_local_limit_distinct_and_nonconstant_projection():
    from dependencies import find_mutation_sites
    from ir import RowLimit, UnionAll
    witness, low, _ = stream_branches()
    bad = [replace(low, limit=1), replace(low, distinct=True),
           replace(low, returns=(Alias(Property('l', 'uid'), 'value'),))]
    for branch in bad:
        query = RowLimit(UnionAll((witness, branch)), 2)
        assert not find_mutation_sites(query, 'S6', 'bounded_demand')


def checked_short_circuit_case(pattern='S6', *, witness_count=2):
    from dependencies import find_mutation_sites
    from ir import RowLimit, UnionAll
    from population import evaluate_query
    from query import render_query
    from state import GraphSpec
    anchor = Node('u', ('Root',), (('id', Literal(1)),))
    def branch(label, rel, variable):
        return Query(anchor, None, (Alias(Literal(1), 'value'),), matches=(
            Match(Edge(Node('u'), Relationship(type=rel), Node(variable, (label,)))),))
    witness, low, high = branch('Witness', 'W', 'w'), branch('Small', 'L', 'l'), branch('Large', 'H', 'h')
    union = UnionAll((witness, low))
    query = (RowLimit(union, 2) if pattern == 'S6' else
             Query(anchor, None, (Alias(Exists(body=union), 'ok'),)))
    groups = [{'count': 1, 'labels': ['Root'], 'properties': {'uid': 1, 'id': 1}},
              {'count': 3, 'labels': ['Large'], 'properties': {'uid': {'affine': {'base': 20, 'step': 1}}}}]
    edges = [{'count': 3, 'source': 1, 'target': {'affine': {'base': 20, 'step': 1}},
              'source_label': 'Root', 'target_label': 'Large', 'type': 'H'}]
    if witness_count:
        groups.append({'count': witness_count, 'labels': ['Witness'],
                       'properties': {'uid': {'affine': {'base': 2, 'step': 1}}}})
        edges.append({'count': witness_count, 'source': 1, 'target': {'affine': {'base': 2, 'step': 1}},
                      'source_label': 'Root', 'target_label': 'Witness', 'type': 'W'})
    spec = GraphSpec('AST', 3, 0, version=2, include_reference=False,
                     node_groups=tuple(groups), edge_groups=tuple(edges))
    check_query = Query(anchor, None, (Alias(Property('u', 'uid'), 'anchor_uid'),
                                       Alias(Count(body=witness), 'witness_rows')))
    check = {'query': render_query(check_query), 'expected_rows': evaluate_query(check_query, spec)}
    site, = find_mutation_sites(query, pattern)
    bindings = {'paper_pattern': pattern, 'form': site.form,
                'mutation_site': list(site.address), 'replacement': to_data(high),
                'union_site': site.guards['union_site'], 'witness_site': site.guards['witness_site'],
                'witness_check': check, 'required_state_checks': [check],
                'validation_graph_spec': spec.to_dict()}
    return query, high, site, bindings, spec


@pytest.mark.parametrize('pattern,operator', [('S6', 'RowLimit'), ('S8', 'Exists')])
def test_short_circuit_replacement_preserves_the_envelope_and_unmodified_witness(pattern, operator):
    from population import evaluate_query
    query, high, site, bindings, spec = checked_short_circuit_case(pattern)
    changed = mutate(query, pattern, bindings=bindings)
    assert node_at(changed, site.address) == high
    witness_address = tuple(site.guards['witness_site'])
    assert node_at(changed, witness_address) == node_at(query, witness_address)
    # QL and QH have different bags; the proof must begin above the UNION.
    assert evaluate_query(node_at(query, site.address), spec) == []
    assert len(evaluate_query(high, spec)) == 3
    proof = propagate_equivalence(query, site, bindings)
    assert proof[0]['operator'] == operator
    assert proof[0]['address'] == site.guards['envelope_site']
    assert proof[0]['replacement_bags_may_differ']
    assert proof[0]['witness']['proof_model_checked']
    assert proof[0]['witness']['database_check_required']


@pytest.mark.parametrize('pattern', ['S6', 'S8'])
@pytest.mark.parametrize('corruption', ['missing_check', 'not_mandatory', 'forged_count', 'different_witness',
                                      'different_anchor', 'wrong_site', 'wrong_union', 'null_union'])
def test_short_circuit_requires_the_actual_per_binding_witness(pattern, corruption):
    from query import parse_query, render_query
    query, _, site, bindings, _ = checked_short_circuit_case(pattern)
    check = bindings['witness_check']
    if corruption == 'missing_check':
        del bindings['witness_check']
    elif corruption == 'not_mandatory':
        bindings['required_state_checks'] = []
    elif corruption == 'forged_count':
        check['expected_rows'] = [{'anchor_uid': 1, 'witness_rows': 999}]
    elif corruption == 'different_witness':
        tree = parse_query(check['query'])
        fake = replace(node_at(query, tuple(site.guards['witness_site'])), where=Literal(False))
        check['query'] = render_query(replace(tree, returns=(tree.returns[0], Alias(Count(body=fake), 'witness_rows'))))
    elif corruption == 'different_anchor':
        tree = parse_query(check['query'])
        check['query'] = render_query(replace(tree, pattern=replace(tree.pattern, labels=('Wrong',))))
    elif corruption == 'wrong_site':
        bindings['mutation_site'] = site.guards['witness_site']
    elif corruption == 'wrong_union':
        bindings['union_site'] = []
    else:
        bindings['union_site'] = None
    with pytest.raises(QueryError):
        mutate(query, pattern, bindings=bindings)


@pytest.mark.parametrize('pattern,rows', [('S6', 1), ('S8', 0)])
def test_short_circuit_rejects_a_real_but_insufficient_witness(pattern, rows):
    query, _, _, bindings, _ = checked_short_circuit_case(pattern, witness_count=rows)
    with pytest.raises(QueryError, match='witness requirement'):
        mutate(query, pattern, bindings=bindings)


@pytest.mark.parametrize('pattern', ['S6', 'S8'])
@pytest.mark.parametrize('corruption', ['limit', 'distinct', 'schema', 'count'])
def test_short_circuit_rejects_blocking_or_schema_changing_replacements(pattern, corruption):
    query, high, _, bindings, _ = checked_short_circuit_case(pattern)
    if corruption == 'limit':
        high = replace(high, limit=1)
    elif corruption == 'distinct':
        high = replace(high, distinct=True)
    elif corruption == 'schema':
        high = replace(high, returns=(Alias(Literal(1), 'other'),))
    else:
        high = replace(high, returns=(Alias(Count(body=Query(Node('x'), None, (Literal(1),))), 'value'),))
    bindings['replacement'] = to_data(high)
    with pytest.raises(QueryError):
        mutate(query, pattern, bindings=bindings)


def test_new_row_limits_do_not_bypass_existing_unordered_limit_guards():
    from dependencies import find_mutation_sites
    from ir import RowLimit, UnionAll
    source = RowLimit(UnionAll((path_query(), path_query())), 1)
    assert not find_p3_sites(source)
    assert find_patterns(source) == ()
    boolean = Query(None, None, (Alias(Boolean('AND', (Literal(True), Literal(False))), 'ok'),))
    assert not find_mutation_sites(RowLimit(boolean, 1), 'P4')
    assert not find_mutation_sites(RowLimit(shared_branches_query(), 1), 'P10')


@pytest.mark.parametrize('pattern', ['S6', 'S8'])
def test_short_circuit_pattern_detection_uses_its_evidence_and_recursive_streams_are_safe(pattern):
    from ir import RowLimit, UnionAll
    query, _, _, bindings, _ = checked_short_circuit_case(pattern)
    assert pattern in find_patterns(query, bindings)
    assert pattern not in find_patterns(query)
    branch = Query(None, None, (Alias(Literal(1), 'value'),))
    assert find_patterns(UnionAll((branch, UnionAll((branch, branch))))) == ()
    assert find_patterns(UnionAll((branch, RowLimit(branch, 1)))) == ()


def test_s6_nested_under_count_and_with_proves_the_actual_consumer_chain():
    from dependencies import find_mutation_sites
    base, _, _, bindings, spec = checked_short_circuit_case()
    query = Query(None, None, (Variable('total'),), pipeline=(
        With((Alias(Count(body=base), 'total'),), where=Comparison(Variable('total'), '=', Literal(2))),))
    site, = find_mutation_sites(query, 'S6')
    bindings.update(mutation_site=list(site.address), union_site=site.guards['union_site'],
                    witness_site=site.guards['witness_site'])
    changed = mutate(query, 'S6', bindings=bindings)
    from population import evaluate_query
    assert evaluate_query(query, spec) == evaluate_query(changed, spec) == [{'total': 2}]
    proof = propagate_equivalence(query, site, bindings)
    assert [step['operator'] for step in proof] == ['RowLimit', 'Count', 'Alias', 'With', 'Query']
    assert proof[1]['relation'] == 'integer_equal'
    data = analyze_dependencies(query).to_dict()
    assert any(use['consumer'] == 'Count' and use['producer'].endswith(':row_limit')
               for use in data['row_uses'])


def test_s8_nested_in_a_with_alias_uses_the_preserved_lexical_anchor():
    from dependencies import find_mutation_sites
    base, _, _, bindings, spec = checked_short_circuit_case('S8')
    query = Query(None, None, (Variable('ok'),), pipeline=(
        Match(base.pattern), With((Alias(base.returns[0].expression, 'ok'),)),))
    site, = find_mutation_sites(query, 'S8')
    bindings.update(mutation_site=list(site.address), union_site=site.guards['union_site'],
                    witness_site=site.guards['witness_site'])
    from population import evaluate_query
    assert evaluate_query(mutate(query, 'S8', bindings=bindings), spec) == [{'ok': True}]
    proof = propagate_equivalence(query, site, bindings)
    assert [step['operator'] for step in proof] == ['Exists', 'Alias', 'With', 'Query']
    imports = proof[0]['witness']['outer_imports']
    assert len(imports) == 1 and imports[0].startswith('root:query::u@root/pipeline/0/pattern/variable')


def test_witness_check_cannot_replace_the_actual_anchor_with_a_same_named_domain():
    from dependencies import find_mutation_sites
    base, _, _, bindings, _ = checked_short_circuit_case('S8')
    query = replace(base, pattern=Node('u', ('OtherRoot',), (('id', Literal(1)),)))
    site, = find_mutation_sites(query, 'S8')
    bindings.update(mutation_site=list(site.address), union_site=site.guards['union_site'],
                    witness_site=site.guards['witness_site'])
    with pytest.raises(QueryError, match='actual imported anchor domain'):
        mutate(query, 'S8', bindings=bindings)


@pytest.mark.parametrize('constant', [True, 1.0])
def test_s6_constant_projection_identity_includes_the_literal_type(constant):
    from dependencies import find_mutation_sites
    from ir import RowLimit, UnionAll
    witness, low, _ = stream_branches()
    low = replace(low, returns=(Alias(Literal(constant), 'value'),))
    assert not find_mutation_sites(RowLimit(UnionAll((witness, low)), 1), 'S6')
    query, high, _, bindings, _ = checked_short_circuit_case()
    bindings['replacement'] = to_data(replace(high, returns=low.returns))
    with pytest.raises(QueryError, match='constant projection|schema'):
        mutate(query, 'S6', bindings=bindings)


def test_s8_replacement_cannot_silently_change_a_union_output_kind():
    query, high, _, bindings, _ = checked_short_circuit_case('S8')
    bindings['replacement'] = to_data(replace(high, returns=(Alias(Literal(True), 'value'),)))
    with pytest.raises(QueryError, match='schema'):
        mutate(query, 'S8', bindings=bindings)


def test_recursive_union_dependency_capture_distinguishes_branch_local_rebinding():
    from ir import UnionAll
    first = Query(Node('u'), None, (Alias(Literal(1), 'value'),))
    second = Query(None, None, (Alias(Variable('u'), 'value'),),
                   pipeline=(With((Alias(Literal(1), 'u'),)),))
    source = Query(Node('u', ('Root',)), None, (Alias(Count(body=UnionAll((first, second))), 'n'),))
    data = analyze_dependencies(source).to_dict()
    branch_scopes = [scope for scope in data['scopes'] if scope['kind'] == 'query'
                     and '/branches/' in scope['node_id']]
    assert len(branch_scopes[0]['imports']) == 1
    assert branch_scopes[1]['imports'] == []
    outer = branch_scopes[0]['imports'][0]
    assert branch_scopes[1]['input_bindings']['u'] == outer
    local = next(symbol for symbol in data['symbols'] if symbol['name'] == 'u'
                 and symbol['kind'] == 'scalar')
    assert local['symbol_id'] != outer
    assert any(use['symbol_id'] == local['symbol_id'] and use['name'] == 'u' for use in data['uses'])


@pytest.mark.parametrize('pattern,form,tree', [
    ('P1', 'edge_mirror', Query(Edge(Node('a'), Relationship(type='R'), Node('b'), 'undirected'), None, (Literal(1),))),
    ('P2', 'inverse_edge', Query(Edge(Node('a'), Relationship(type='F'), Node('b')), None, (Literal(1),))),
    ('P5', 'or', Query(None, None, (Alias(Boolean('OR', (Literal(True), Literal(False))), 'v'),))),
    ('P6', 'equality', Query(Node('a'), Comparison(Property('a', 'k'), '=', Literal(1)), (Literal(1),))),
    ('P7', 'labels', Query(Node('a', ('A', 'B')), None, (Literal(1),))),
    ('P8', 'types', Query(Edge(Node('a'), Relationship(type='R', alternatives=('S',)), Node('b')), None, (Literal(1),))),
    ('P9', 'predicate_placement', Query(Node('a'), Comparison(Property('a', 'k'), '=', Literal(1)), (Literal(1),), matches=(Match(Node('b')),))),
    ('P10', 'independent', Query(Node('a'), None, (Literal(1),), matches=(Match(Node('b')),))),
    ('P10', 'chain', Query(Edge(Node('a'), Relationship(type='R'), Node('b')), None, (Literal(1),), matches=(Match(Edge(Node('b'), Relationship(type='S'), Node('c'))),))),
    ('P11', 'optional_branches', Query(Node('u'), None, (Literal(1),), matches=(Match(Edge(Node('u'), Relationship(type='R'), Node('a')), optional=True), Match(Edge(Node('u'), Relationship(type='S'), Node('b')), optional=True)))),
    ('P12', 'union_all', __import__('ir').UnionAll((Query(None,None,(Alias(Literal(1),'v'),)), Query(None,None,(Alias(Literal(2),'v'),))))),
    ('P13', 'case_dual', Query(None, None, (Alias(__import__('ir').Case(Literal(None),Literal(1),Literal(2)), 'v'),))),
])
def test_all_paper_symmetries_are_locally_discovered_under_a_recursive_consumer(pattern, form, tree):
    from dependencies import find_mutation_sites
    from mutator import plan_mutation
    nested = Query(None, None, (Alias(Count(body=tree), 'n'),))
    site, = find_mutation_sites(nested, pattern, form)
    changed = plan_mutation(nested, site, inverse_types=('F','R'))
    assert changed != nested
    assert changed.returns[0].name == 'n'
    if pattern == 'P2':
        with pytest.raises(QueryError, match='pairing evidence'):
            propagate_equivalence(nested, site)
    else:
        assert propagate_equivalence(nested, site)[-1]['relation'] == 'bag_equal'


def generic_short_circuit_case(pattern):
    from dependencies import find_mutation_sites, premise_obligations
    from ir import Case, ConditionalQuery, Negation, RowLimit, encode_fragment
    from population import evaluate_query
    from query import render_query
    _, _, _, _, spec = checked_short_circuit_case()
    anchor = Node('u', ('Root',), (('id', Literal(1)),))
    true, false = Comparison(Property('u','id'),'=',Literal(1)), Comparison(Property('u','id'),'=',Literal(2))
    def region(label, kind):
        return Query(Edge(anchor,Relationship(type=kind),Node('item',(label,))),
                     Comparison(Call('coalesce',(Property('item','keep'),Literal(False))),'=',Literal(True)),
                     (Alias(Literal(1),'value'),))
    low, high = region('Small','L'), region('Large','H')
    scalar_low, scalar_high = Count(body=low), Count(body=high)
    if pattern in {'S1','S2'}:
        expr = Boolean('OR' if pattern=='S1' else 'AND', (true if pattern=='S1' else false,
                       Comparison(scalar_low,'>',Literal(0))))
        source = Query(anchor,None,(Alias(expr,'value'),))
        replacement = Comparison(scalar_high,'>',Literal(0))
    elif pattern == 'S3':
        source, replacement = ConditionalQuery(Literal(True),low,low), high
    elif pattern == 'S4':
        source, replacement = Query(anchor,None,(Alias(Case(true,Literal(7),scalar_low),'value'),)), scalar_high
    elif pattern == 'S5':
        source, replacement = RowLimit(low,0), high
    elif pattern == 'S7':
        source, replacement = Query(anchor,None,(Alias(Call('coalesce',(Literal(None),Literal(7),scalar_low)),'value'),)), scalar_high
    elif pattern == 'S9':
        independent = replace(low.pattern,left=Node('r',('OtherRoot',),(('id',Literal(2)),)))
        source = Query(anchor,false,(Property('u','uid'),),matches=(Match(independent,low.where),))
        replacement = Match(replace(high.pattern,left=independent.left),high.where)
    else:
        source = Query(anchor,Boolean('AND',(false,Negation(Exists(body=low)))),(Property('u','uid'),),distinct=True)
        replacement = high
    site, = (site for site in find_mutation_sites(source,pattern)
             if pattern != 'S7' or site.address == ('returns', 0, 'expression', 'args', 2))
    checks=[]
    for obligation in premise_obligations(source,site):
        q=Query(anchor,None,(Alias(Property('u','uid'),'anchor_uid'),Alias(obligation['expression'],'premise')))
        checks.append({'query':render_query(q),'expected_rows':evaluate_query(q,spec)})
    bindings={'paper_pattern':pattern,'form':site.form,'mutation_site':list(site.address),
              'replacement':encode_fragment(replacement),'premise_checks':checks,
              'required_state_checks':checks,'validation_graph_spec':spec.to_dict()}
    return source,site,bindings,spec


@pytest.mark.parametrize('pattern',['S1','S2','S3','S4','S5','S7','S9','S10'])
def test_all_other_short_circuit_laws_replace_only_the_designated_fragment(pattern):
    from population import evaluate_query
    source,site,bindings,spec=generic_short_circuit_case(pattern)
    changed=mutate(source,pattern,bindings=bindings)
    assert source != changed
    assert evaluate_query(source,spec)==evaluate_query(changed,spec)
    proof=propagate_equivalence(source,site,bindings)
    assert proof[0]['replacement_values_or_bags_may_differ']
    if pattern!='S5':
        assert proof[0]['evidence']['database_check_required']


@pytest.mark.parametrize('pattern',['S1','S2','S3','S4','S7','S9','S10'])
def test_decisive_premises_cannot_be_forged_or_omitted(pattern):
    source,_,bindings,_=generic_short_circuit_case(pattern)
    bindings['required_state_checks']=[]
    with pytest.raises(QueryError,match='mandatory'):
        mutate(source,pattern,bindings=bindings)


def test_membership_null_safety_must_protect_the_actual_filter():
    from dependencies import find_mutation_sites
    source,site,_,_=generic_short_circuit_case('S10')
    body=node_at(source,site.address)
    body=replace(body,where=Comparison(Property('item','keep'),'=',Literal(True)),
                 returns=(Alias(Call('coalesce',(Property('item','keep'),Literal(False))),'value'),))
    assert not find_mutation_sites(replace_at(source,site.address,body),'S10')


def inverse_case(full_count=6000):
    from dependencies import find_mutation_sites
    from state import GraphSpec, render_state_checks
    def graph(count):
        uid={'affine':{'base':10,'step':1}}
        pair={'affine':{'base':100,'step':1}}
        return GraphSpec('AST',count,0,version=2,include_reference=False,node_groups=(
            {'count':1,'labels':['Root'],'properties':{'uid':1,'id':1}},
            {'count':count,'labels':['Target'],'properties':{'uid':uid}}),edge_groups=(
                {'count':count,'source':1,'target':uid,'source_label':'Root','target_label':'Target','type':'F','properties':{'pair':pair}},
                {'count':count,'source':uid,'target':1,'source_label':'Target','target_label':'Root','type':'R','properties':{'pair':pair}}))
    proof,full=graph(2),graph(full_count)
    source=Query(Edge(Node('u',('Root',),(('id',Literal(1)),)),Relationship(type='F'),Node('v',('Target',))),None,
                 (Property('u','uid'),Property('v','uid')))
    site,=find_mutation_sites(source,'P2')
    checks=[]
    for kind,other in (('F','R'),('R','F')):
        checks.extend([{'kind':'edge_count','type':kind,'expected':full_count},
                       {'kind':'distinct_values','type':kind,'property':'pair','expected':full_count},
                       {'kind':'inverse_edges','forward':kind,'reverse':other,'pair_key':'pair','expected':0}])
    checks=render_state_checks(checks)
    bindings={'paper_pattern':'P2','form':site.form,'mutation_site':list(site.address),
              'inverse_relationship':{'forward_type':'F','reverse_type':'R','pair_key':'pair'},
              'inverse_checks':checks,'required_state_checks':checks,
              'validation_graph_spec':proof.to_dict(),'inverse_graph_spec':full.to_dict()}
    return source,site,bindings,proof


def test_inverse_pairing_is_proved_for_small_and_affine_large_states():
    from population import evaluate_query
    source,site,bindings,proof=inverse_case()
    changed=mutate(source,'P2',bindings=bindings)
    assert changed.pattern.direction=='in' and changed.pattern.relationship.type=='R'
    assert evaluate_query(source,proof)==evaluate_query(changed,proof)
    evidence=propagate_equivalence(source,site,bindings)[0]['pairing']
    assert evidence['compact_full_graph_checked'] and evidence['database_check_required']


@pytest.mark.parametrize('change',['duplicate_key','wrong_endpoints','missing_check','forged_count'])
def test_inverse_pairing_rejects_semantic_or_evidence_drift(change):
    source,_,bindings,_=inverse_case()
    if change=='duplicate_key':
        bindings['inverse_graph_spec']['edge_groups'][0]['properties']['pair']=100
    elif change=='wrong_endpoints':
        bindings['inverse_graph_spec']['edge_groups'][1]['target']=2
    elif change=='missing_check':
        bindings['required_state_checks']=bindings['required_state_checks'][:-1]
    else:
        bindings['inverse_checks'][0]['expected_rows']=[{'count':2}]
    with pytest.raises(QueryError):
        mutate(source,'P2',bindings=bindings)


def test_decisive_alias_is_expanded_through_its_actual_scalar_producer():
    from dependencies import find_mutation_sites,premise_obligations
    from population import evaluate_query
    from query import render_query
    source,_,bindings,spec=generic_short_circuit_case('S1')
    boolean=source.returns[0].expression
    query=Query(None,None,(Alias(replace(boolean,terms=(Variable('gate'),boolean.terms[1])),'value'),),pipeline=(
        Match(source.pattern),With((Variable('u'),Alias(boolean.terms[0],'gate')))))
    site,=find_mutation_sites(query,'S1')
    obligation,=premise_obligations(query,site)
    assert len(obligation['imports'])==1
    checked=Query(source.pattern,None,(Alias(Property('u','uid'),'anchor_uid'),Alias(obligation['expression'],'premise')))
    check={'query':render_query(checked),'expected_rows':evaluate_query(checked,spec)}
    bindings.update(mutation_site=list(site.address),premise_checks=[check],required_state_checks=[check])
    assert evaluate_query(mutate(query,'S1',bindings=bindings),spec)==[{'value':True}]
    # The gate still comes from the first u after a same-spelled new u is bound.
    rebound=replace(query,pipeline=(query.pipeline[0],With((Alias(boolean.terms[0],'gate'),)),
                                   Match(Node('u',('OtherRoot',),(('id',Literal(2)),)))))
    site,=find_mutation_sites(rebound,'S1')
    obligation,=premise_obligations(rebound,site)
    wrong=Query(rebound.pipeline[-1].pattern,None,
                (Alias(Property('u','uid'),'anchor_uid'),Alias(obligation['expression'],'premise')))
    check={'query':render_query(wrong),'expected_rows':[{'anchor_uid':2,'premise':True}]}
    bindings.update(mutation_site=list(site.address),premise_checks=[check],required_state_checks=[check])
    with pytest.raises(QueryError,match='imported anchor domain'):
        mutate(rebound,'S1',bindings=bindings)


def test_premise_check_must_not_capture_an_uncorrelated_count_local():
    from dependencies import find_mutation_sites,premise_obligations
    from ir import ConditionalQuery,encode_fragment
    from population import evaluate_query
    from query import render_query
    _,_,_,spec=generic_short_circuit_case('S3')
    branch=Query(None,None,(Alias(Literal(1),'value'),))
    guard=Comparison(Count(body=Query(Node('u',('Root',)),None,(Literal(1),))),'=',Literal(1))
    source=ConditionalQuery(guard,branch,branch)
    site,=find_mutation_sites(source,'S3')
    obligation,=premise_obligations(source,site)
    checked=Query(Node('u',('Root',),(('id',Literal(1)),)),None,
                  (Alias(Property('u','uid'),'anchor_uid'),Alias(obligation['expression'],'premise')))
    check={'query':render_query(checked),'expected_rows':evaluate_query(checked,spec)}
    bindings={'paper_pattern':'S3','mutation_site':list(site.address),'replacement':encode_fragment(replace(branch,returns=(Alias(Literal(2),'value'),))),
              'premise_checks':[check],'required_state_checks':[check],'validation_graph_spec':spec.to_dict()}
    with pytest.raises(QueryError,match='capture a local graph binding'):
        mutate(source,'S3',bindings=bindings)


@pytest.mark.parametrize('pattern',['S1','S2','S4','S7'])
def test_unselected_scalar_alternatives_allow_compatible_null(pattern):
    from ir import encode_fragment
    from population import evaluate_query
    source,_,bindings,spec=generic_short_circuit_case(pattern)
    bindings['replacement']=encode_fragment(Literal(None))
    changed=mutate(source,pattern,bindings=bindings)
    assert evaluate_query(source,spec)==evaluate_query(changed,spec)


def test_generic_sites_record_the_actual_with_scope_instead_of_the_syntactic_query_parent():
    from dependencies import find_mutation_sites
    query=Query(None,None,(Alias(Boolean('OR',(Literal(True),Literal(False))),'value'),),pipeline=(
        With((Alias(Literal(1),'n'),)),
        Match(Edge(Node('a'),Relationship(type='R'),Node('b'),'undirected')),
        Match(Node('c'))))
    scope=analyze_dependencies(query).scopes[1]['scope_id']
    for pattern in ('P1','P5'):
        site,=find_mutation_sites(query,pattern)
        assert site.scope_id==scope
    site,=find_mutation_sites(query,'P10','independent')
    assert site.scope_id==scope
