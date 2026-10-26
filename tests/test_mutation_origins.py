"""Explicit law marks and occurrence provenance survive legal nested copies."""

from dataclasses import fields, is_dataclass

import pytest

import ir
from dependencies import find_mutation_sites, node_at, select_mutation_site
from mutator import plan_mutation, plan_mutation_with_correspondence


def nodes(tree, address=()):
    if isinstance(tree, ir.Node):
        yield address, tree
    if is_dataclass(tree):
        for field in fields(tree):
            yield from nodes(getattr(tree, field.name), (*address, field.name))
    elif isinstance(tree, tuple):
        for index, value in enumerate(tree):
            yield from nodes(value, (*address, index))


def chain(length=4):
    vertices = [ir.Node(name, ('Shared',)) for name in ('u', 'v', 'w', 'n', 'r')[:length + 1]]
    edges = [ir.Edge(left, ir.Relationship(type='LINK'), right)
             for left, right in zip(vertices, vertices[1:])]
    return ir.Query(edges[0], None, tuple(ir.Property(node.variable, 'uid') for node in vertices),
                    matches=tuple(ir.Match(edge) for edge in edges[1:]))


def test_explicit_mark_selects_one_pair_in_a_same_label_long_chain():
    source = chain()
    sites = find_mutation_sites(source, 'P10', 'chain')
    assert len(sites) == 3
    marked = select_mutation_site(source, 'P10', 'chain', sites[1].address)
    assert marked == sites[1]
    changed, correspondence = plan_mutation_with_correspondence(source, marked)
    assert changed == plan_mutation(source, marked)
    assert len(correspondence) == len(list(nodes(source))) == len(list(nodes(changed)))
    for item in correspondence:
        assert item.equal is True
        assert item.left_address[0] == 'query'
        assert item.right_address[:2] == ('alternatives', 0)
        before = node_at(source, item.left_address[1:])
        after = node_at(changed, item.right_address[2:])
        assert before == after
    # The middle pair swaps occurrences. Identical schema names do not select
    # the source port, and the unchanged suffix keeps its own original port.
    mapping = {item.right_address[2:]: item.left_address[1:] for item in correspondence}
    assert mapping[('matches', 0, 'pattern', 'left')] == ('matches', 1, 'pattern', 'left')
    assert mapping[('matches', 1, 'pattern', 'left')] == ('matches', 0, 'pattern', 'left')
    assert mapping[('matches', 2, 'pattern', 'left')] == ('matches', 2, 'pattern', 'left')


@pytest.mark.parametrize('address', [None, ('missing',), ('matches', True), ('returns', -1), ('returns', 0)])
def test_an_invalid_or_nonapplicable_explicit_mark_is_rejected(address):
    with pytest.raises(ir.QueryError, match='mark|address|applicable'):
        select_mutation_site(chain(), 'P10', 'chain', address)


def test_nested_conditionals_keep_scope_distinct_origins_and_select_the_inner_site():
    # Both branches deliberately reuse one immutable AST object. Each lexical
    # occurrence still has a distinct origin address, rather than an object ID.
    row = ir.Query(ir.Node('item', ('Shared',)), None, (ir.Alias(ir.Literal(1), 'c'),))
    inner = ir.ConditionalQuery(ir.Literal(True), row, row)
    source = ir.ConditionalQuery(ir.Literal(True), inner, row)
    assert len(find_mutation_sites(source, 'S3')) == 2
    site = select_mutation_site(source, 'S3', 'conditional', ('then', 'otherwise'))
    replacement = ir.Query(ir.Node('item', ('Shared',)), None, (ir.Alias(ir.Literal(2), 'c'),))
    changed, correspondence = plan_mutation_with_correspondence(source, site, replacement=replacement)
    mapping = {item.right_address[2:]: item.left_address[1:] for item in correspondence}
    assert mapping == {
        ('then', 'then', 'pattern'): ('then', 'then', 'pattern'),
        ('otherwise', 'pattern'): ('otherwise', 'pattern'),
    }
    assert ('then', 'otherwise', 'pattern') not in mapping
    assert changed.then.otherwise == replacement


def test_moving_the_first_match_does_not_reassign_unrelated_return_subqueries():
    source = chain(2)
    projection = ir.Alias(ir.Count(body=ir.Query(ir.Node('local', ('Shared',)), None, (ir.Literal(1),))), 'count')
    source = ir.Query(source.pattern, None, (*source.returns, projection), matches=source.matches)
    site = select_mutation_site(source, 'P10', 'chain', ())
    _, correspondence = plan_mutation_with_correspondence(source, site)
    mapping = {item.right_address[2:]: item.left_address[1:] for item in correspondence}
    address = ('returns', 3, 'expression', 'body', 'pattern')
    assert mapping[address] == address


def test_chain_equivalence_record_uses_shared_binding_bag_join():
    from dependencies import propagate_equivalence
    source = chain(2)
    site = select_mutation_site(source, 'P10', 'chain', ())
    local = propagate_equivalence(source, site)[0]
    assert local['premise'] == 'mandatory_bag_join_commutativity_on_shared_bindings'
    assert local['join_symbols'] == [site.guards['chain_joint_symbol']]
