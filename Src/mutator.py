"""Symmetric rewrites and graph-specific SCor reference substitutions."""

from dataclasses import fields, is_dataclass, replace

from query import (
    Alias, All, Boolean, Call, Case, Comparison, Concat, ConditionalQuery, Count, Edge, Exists,
    ListExpr, Literal, Negation, Node, Property, Query, QueryError,
    UnionAll, Variable, With, Match, clauses, expression_variables, pattern_variables,
    validate_query, with_clauses,
)
from dependencies import (analyze_dependencies, find_mutation_sites, find_p3_sites, node_at, replace_at,
                          select_mutation_site,
                          verify_short_circuit_witness, verify_rule_premises, verify_inverse_pair, site_environment)


PATTERNS = {
    'P1': 'single-edge endpoint mirror',
    'P2': 'paired inverse relationship types',
    'P3': 'bounded-path endpoint mirror',
    'P4': 'AND operand order',
    'P5': 'OR operand order',
    'P6': 'comparison operand duality',
    'P7': 'node-label order',
    'P8': 'relationship-type order',
    'P9': 'predicate attachment',
    'P10': 'independent MATCH order',
    'P11': 'shared-anchor branch order',
    'P12': 'chain MATCH order',
    'P13': 'independent OPTIONAL MATCH order',
    'P14': 'UNION ALL branch order',
    'P15': 'CASE branch duality',
    'P16': 'quantifier input-group order',
    'S1': 'SCor OR: skip expensive graph count',
    'S2': 'SCor AND: skip expensive graph count',
    'S3': 'SCor IF: conditional graph query',
    'S4': 'SCor CASE: skip unselected graph count',
    'S5': 'SCor skipped graph scan',
    'S6': 'SCor limited graph scan',
    'S7': 'SCor COALESCE: skip graph fallback',
    'S8': 'SCor FirstMatch: graph semi-join',
    'S9': 'SCor empty-input graph join',
    'S10': 'SCor empty graph-set operation',
}

SCOR_PAPER_PATTERNS = dict(zip((f'S{i}' for i in range(1, 11)),
                              ('1.1', '1.2', '2.1', '2.2', '3.1', '3.2', '4.1', '4.2', '5.1', '5.2')))


def _children(expression):
    if isinstance(expression, Comparison):
        return expression.left, expression.right
    if isinstance(expression, Boolean):
        return expression.terms
    if isinstance(expression, Negation):
        return (expression.expression,)
    if isinstance(expression, (Exists, Count)):
        if expression.body is not None:
            return (expression.body,)
        return () if expression.where is None else (expression.where,)
    if isinstance(expression, Query):
        return (*clauses(expression), *expression.returns)
    if isinstance(expression, Match):
        return () if expression.where is None else (expression.where,)
    if isinstance(expression, With):
        return (*expression.projections, *((expression.where,) if expression.where is not None else ()))
    if isinstance(expression, UnionAll):
        return expression.branches
    if type(expression).__name__ == 'RowLimit':
        return (expression.body,)
    if isinstance(expression, ConditionalQuery):
        return expression.condition, expression.then, expression.otherwise
    if isinstance(expression, Call):
        return expression.args
    if isinstance(expression, Alias):
        return (expression.expression,)
    if isinstance(expression, Case):
        return expression.condition, expression.then, expression.otherwise
    if isinstance(expression, ListExpr):
        return expression.items
    if isinstance(expression, Concat):
        return expression.left, expression.right
    if isinstance(expression, All):
        return expression.items, expression.predicate
    return ()


def _pure(expression):
    if expression is None:
        return True
    if isinstance(expression, (Literal, Property, Variable)):
        return True
    if isinstance(expression, Call):
        if expression.name.lower() not in {'length', 'coalesce', 'range', 'size', 'nodes'}:
            return False
        if expression.name.lower() == 'range':
            return (len(expression.args) in (2, 3)
                    and all(isinstance(arg, Literal) and type(arg.value) is int for arg in expression.args)
                    and (len(expression.args) == 2 or expression.args[2].value != 0))
    if not isinstance(expression, (Comparison, Boolean, Negation, Exists, Count, Call, Alias, Case, ListExpr, Concat,
                                   All, Query, Match, With, UnionAll, ConditionalQuery)):
        return False
    return all(_pure(child) for child in _children(expression))


def _variables(clause):
    return set(pattern_variables(clause.pattern))


def _local_predicate(clause, bound=()):
    return clause.where is None or expression_variables(clause.where) <= (_variables(clause) | set(bound))


def _independent_branches(parts, optional):
    if len(parts) != 3 or parts[0].optional or not isinstance(parts[0].pattern, Node):
        return False
    anchor = _variables(parts[0])
    left, right = parts[1:]
    if left.optional != optional or right.optional != optional:
        return False
    a, b = _variables(left), _variables(right)
    return (a & b == anchor and anchor <= a and anchor <= b
            and bool(a - anchor) and bool(b - anchor)
            and _local_predicate(left, anchor) and _local_predicate(right, anchor))


def _movable_predicate(parts):
    if len(parts) != 2 or any(part.optional for part in parts):
        return False
    if (parts[0].where is None) == (parts[1].where is None):
        return False
    predicate = parts[0].where if parts[0].where is not None else parts[1].where
    return expression_variables(predicate) <= (_variables(parts[0]) & _variables(parts[1]))


def _chain(parts):
    if len(parts) != 2 or any(part.optional or not isinstance(part.pattern, Edge) for part in parts):
        return False
    a, b = (part.pattern for part in parts)
    if any(edge.relationship.min_hops is not None or edge.direction != 'out' for edge in (a, b)):
        return False
    shared = _variables(parts[0]) & _variables(parts[1])
    return (len(shared) == 1
            and (a.right.variable == b.left.variable or b.right.variable == a.left.variable)
            and all(_local_predicate(part) for part in parts))


def _inverse_types(bindings):
    inverse = (bindings or {}).get('inverse_relationship', {})
    forward, reverse = inverse.get('forward_type'), inverse.get('reverse_type')
    if (not isinstance(forward, str) or not isinstance(reverse, str)
            or not forward or not reverse or forward == reverse or not inverse.get('pair_key')):
        return None
    return forward, reverse


def _label_site(pattern):
    nodes = (pattern,) if isinstance(pattern, Node) else (pattern.left, pattern.right) if isinstance(pattern, Edge) else ()
    return next((index for index, node in enumerate(nodes) if len(set(node.labels)) > 1), None)


def _return_site(query, kind):
    return next((index for index, item in enumerate(query.returns)
                 if isinstance(item, Alias) and isinstance(item.expression, kind)), None)


def _edges(value):
    if isinstance(value, Edge):
        yield value
    elif isinstance(value, tuple):
        for item in value:
            yield from _edges(item)
    elif is_dataclass(value):
        for field in fields(value):
            yield from _edges(getattr(value, field.name))


def _high_count(value, high):
    return sum(edge.relationship.type == high for edge in _edges(value))


def _only_high_inside(query, parts, high):
    count = _high_count(query, high)
    return count > 0 and count == sum(_high_count(part, high) for part in parts)


def _replace_high(value, high, reference):
    """Replace relationship references in the AST, never labels inside strings."""
    if isinstance(value, Edge):
        if value.relationship.type == high:
            return replace(value, relationship=replace(value.relationship, type=reference))
        return value
    if isinstance(value, tuple):
        return tuple(_replace_high(item, high, reference) for item in value)
    if is_dataclass(value):
        return replace(value, **{field.name: _replace_high(getattr(value, field.name), high, reference)
                                 for field in fields(value)})
    return value


def _literal_is(expression, value):
    return isinstance(expression, Literal) and type(expression.value) is type(value) and expression.value == value


def _known_value(expression, environment=None, root_facts=None, spec=None):
    """Evaluate the small total guard language under explicit checked facts.

    This is not a property-value guesser or a general Cypher evaluator. A
    property needs its variable and key in the checked environment. Only a
    singleton-root node COUNT is supported; expensive edge counts are unknown.
    """
    environment = environment or {}
    if isinstance(expression, Literal):
        return True, expression.value
    if isinstance(expression, Property):
        values = environment.get(expression.variable, {})
        return (True, values[expression.key]) if expression.key in values else (False, None)
    if isinstance(expression, Count) and isinstance(expression.pattern, Node) and root_facts is not None:
        node = expression.pattern
        if (not _scor_root(node, spec)
                or node.variable in environment and environment[node.variable] != root_facts):
            return False, None
        if expression.where is None:
            return True, 1
        known, value = _known_value(expression.where, {node.variable: root_facts}, root_facts, spec)
        return (True, int(value is True)) if known and type(value) in (bool, type(None)) else (False, None)
    if isinstance(expression, Negation):
        known, value = _known_value(expression.expression, environment, root_facts, spec)
        return ((True, None if value is None else not value)
                if known and type(value) in (bool, type(None)) else (False, None))
    if isinstance(expression, Boolean):
        values = [_known_value(term, environment, root_facts, spec) for term in expression.terms]
        if any(not known or type(value) not in (bool, type(None)) for known, value in values):
            return False, None
        values = [value for _, value in values]
        if expression.operator == 'AND':
            return True, False if False in values else None if None in values else True
        return True, True if True in values else None if None in values else False
    if isinstance(expression, Case):
        known, condition = _known_value(expression.condition, environment, root_facts, spec)
        if known and type(condition) in (bool, type(None)):
            return _known_value(expression.then if condition is True else expression.otherwise,
                                environment, root_facts, spec)
        return False, None
    if isinstance(expression, Call) and expression.name == 'coalesce':
        for argument in expression.args:
            known, value = _known_value(argument, environment, root_facts, spec)
            if not known:
                return False, None
            if value is not None:
                return True, value
        return True, None
    if isinstance(expression, Comparison):
        a, left = _known_value(expression.left, environment, root_facts, spec)
        b, right = _known_value(expression.right, environment, root_facts, spec)
        if not a or not b:
            return False, None
        if expression.operator == 'IS NULL':
            return True, left is None
        if expression.operator == 'IS NOT NULL':
            return True, left is not None
        if left is None or right is None:
            return True, None
        if type(left) != type(right) or type(left) not in (bool, int, float, str):
            return False, None
        operations = {'=': lambda: left == right, '<>': lambda: left != right,
                      '<': lambda: left < right, '<=': lambda: left <= right,
                      '>': lambda: left > right, '>=': lambda: left >= right}
        if expression.operator in operations:
            return True, operations[expression.operator]()
    return False, None


def _constant_truth(expression):
    known, value = _known_value(expression)
    return (True, value) if known and type(value) in (bool, type(None)) else (False, None)


def _constant_is(expression, value):
    known, actual = _constant_truth(expression)
    return known and actual is value


def _equality(predicate, left, right, key):
    pair = (Property(left, key), Property(right, key))
    return (isinstance(predicate, Comparison) and predicate.operator == '='
            and (predicate.left, predicate.right) in (pair, tuple(reversed(pair))))


def _null_safe_equality(predicate, left, right, key):
    if not isinstance(predicate, Boolean) or predicate.operator != 'OR' or len(predicate.terms) != 2:
        return False
    for equal, nulls in (predicate.terms, tuple(reversed(predicate.terms))):
        if (_equality(equal, left, right, key) and isinstance(nulls, Boolean)
                and nulls.operator == 'AND' and len(nulls.terms) == 2):
            expected = {Comparison(Property(left, key), 'IS NULL', Literal(None)),
                        Comparison(Property(right, key), 'IS NULL', Literal(None))}
            if set(nulls.terms) == expected:
                return True
    return False


def _scor_root(node, spec):
    return (isinstance(node, Node)
            and node.labels == (spec.get('root_label', 'ScorRoot'),)
            and len(node.properties) == 1
            and node.properties[0][0] == spec.get('root_key', 'id')
            and _literal_is(node.properties[0][1], spec.get('root_value', 1)))


def _scor_member(edge, spec, *, correlated=False):
    """Cardinality and witness checks describe this exact traversal, not extra filters."""
    return (isinstance(edge, Edge) and edge.direction == 'out'
            and edge.left.variable != edge.right.variable
            and edge.relationship.type == spec['high_type']
            and not edge.relationship.properties and edge.relationship.variable is None
            and not edge.right.properties
            and edge.right.labels == (spec.get('target_label', 'ScorTarget'),)
            and ((not edge.left.labels and not edge.left.properties) if correlated
                 else _scor_root(edge.left, spec)))


def _checked_guard_context(query, spec):
    """Facts are usable only with the exact checks emitted by this fixture.

    The runner executes these checks before timing. Keeping their complete
    query and expected rows in the binding prevents arbitrary runtime
    properties from acquiring a truth value solely through a pattern name.
    """
    facts = spec.get('guard_facts')
    check = {'query': 'MATCH (u:ScorRoot {id: 1}) RETURN u.id AS id, u.uid AS uid, u.key AS key',
             'expected_rows': [{'id': 1, 'uid': 1, 'key': 1}]}
    cardinality = {'query': 'MATCH (u:ScorRoot) RETURN count(u) AS count',
                   'expected_rows': [{'count': 1}]}
    if (facts != {'id': 1, 'uid': 1, 'key': 1}
            or not isinstance(facts, dict) or any(type(value) is not int for value in facts.values())
            or spec.get('guard_check') != check
            or spec.get('guard_cardinality_check') != cardinality
            or spec.get('root_label') != 'ScorRoot' or spec.get('root_key') not in {'id', 'uid'}
            or type(spec.get('root_value')) is not int or spec.get('root_value') != 1):
        return {}, None
    environment = {}
    if isinstance(query, Query):
        for part in clauses(query):
            for variable in pattern_variables(part.pattern):
                environment.setdefault(variable, {})
            node = part.pattern.left if isinstance(part.pattern, Edge) else part.pattern
            if (not part.optional and isinstance(node, Node) and node.labels == ('ScorRoot',)
                    and (not node.properties or _scor_root(node, spec))):
                environment[node.variable] = facts
    return environment, facts


def _scor_pattern(query, bindings):
    """Recognize a source-grounded SCor case whose state facts are checked before execution."""
    spec = (bindings or {}).get('scor', {})
    name = spec.get('pattern')
    if name not in SCOR_PAPER_PATTERNS or spec.get('paper_pattern') != SCOR_PAPER_PATTERNS[name]:
        return None
    high, reference = spec.get('high_type'), spec.get('reference_type')
    if not isinstance(high, str) or not isinstance(reference, str) or not high or not reference or high == reference:
        return None
    big, small = spec.get('high_matches'), spec.get('reference_matches')
    if type(big) is not int or type(small) is not int or not 0 <= small < big:
        return None
    high_edges = [edge for edge in _edges(query) if edge.relationship.type == high]
    if not high_edges or any(edge.relationship.alternatives or edge.relationship.min_hops is not None for edge in high_edges):
        return None
    environment, facts = _checked_guard_context(query, spec)

    def guard_value(expression):
        known, value = _known_value(expression, environment, facts, spec)
        return (True, value) if known and type(value) in (bool, type(None)) else (False, None)

    def guard_is(expression, expected):
        known, value = guard_value(expression)
        return known and value is expected

    if name == 'S3':
        if not isinstance(query, ConditionalQuery):
            return None
        known, condition = guard_value(query.condition)
        if not known:
            return None
        skipped = query.otherwise if condition is True else query.then
        return name if _only_high_inside(query, (skipped,), high) else None
    if isinstance(query, (UnionAll, ConditionalQuery)):
        return None
    parts = clauses(query)
    if any(not _pure(part.where) for part in parts) or any(not _pure(item) for item in query.returns):
        return None
    projections = [item.expression if isinstance(item, Alias) else item for item in query.returns]
    if name in {'S1', 'S2'}:
        operator, absorbing = ('OR', True) if name == 'S1' else ('AND', False)
        for expression in projections:
            if isinstance(expression, Boolean) and expression.operator == operator and len(expression.terms) == 2:
                for cheap, expensive in (expression.terms, tuple(reversed(expression.terms))):
                    if guard_is(cheap, absorbing) and _only_high_inside(query, (expensive,), high):
                        return name
    elif name == 'S4':
        for expression in projections:
            if isinstance(expression, Case):
                known, condition = guard_value(expression.condition)
                if known:
                    skipped = expression.otherwise if condition is True else expression.then
                    if _only_high_inside(query, (skipped,), high):
                        return name
    elif name == 'S5':
        if query.limit == 0 or any(not part.optional and guard_is(part.where, False) for part in parts):
            return name
    elif name == 'S6':
        if (type(query.limit) is int and 0 < query.limit <= min(big, small)
                and not query.distinct and len(parts) == 1 and parts[0].where is None
                and _scor_member(parts[0].pattern, spec) and not parts[0].optional
                and all(isinstance(expression, Literal) for expression in projections)
                and _only_high_inside(query, (parts[0].pattern,), high)):
            return name
    elif name == 'S7':
        for expression in projections:
            if isinstance(expression, Call) and expression.name == 'coalesce':
                for index, argument in enumerate(expression.args):
                    known, value = _known_value(argument, environment, facts, spec)
                    if not known:
                        break
                    if value is not None:
                        if _only_high_inside(query, expression.args[index + 1:], high):
                            return name
                        break
    elif name == 'S8':
        if (len(parts) == 1 and _scor_root(parts[0].pattern, spec) and isinstance(parts[0].where, Exists)
                and big >= 1 and small >= 1 and spec.get('all_candidates_match') is True):
            exists = parts[0].where
            edge = exists.pattern
            key = spec.get('match_key', 'key')
            if (_scor_member(edge, spec, correlated=True) and edge.left.variable == parts[0].pattern.variable
                    and (_equality(exists.where, edge.left.variable, edge.right.variable, key)
                         or exists.where is None or _literal_is(exists.where, True))
                    and _only_high_inside(query, (exists,), high)):
                return name
    elif name == 'S9':
        if (len(parts) >= 2 and not any(part.optional for part in parts)
                and guard_is(parts[0].where, False) and not _high_count(parts[0], high)):
            return name
    elif name == 'S10':
        if not (query.distinct and len(parts) == 1 and isinstance(parts[0].pattern, Node)):
            return None
        predicate = parts[0].where
        if isinstance(predicate, Boolean) and predicate.operator == 'AND' and len(predicate.terms) == 2:
            for guard, membership in (predicate.terms, tuple(reversed(predicate.terms))):
                exists = membership.expression if isinstance(membership, Negation) else membership
                if not guard_is(guard, False) or not isinstance(exists, Exists) or not isinstance(exists.pattern, Edge):
                    continue
                edge = exists.pattern
                key = spec.get('match_key', 'key')
                if (edge.left.variable == parts[0].pattern.variable
                        and _null_safe_equality(exists.where, edge.left.variable, edge.right.variable, key)
                        and _only_high_inside(query, (membership,), high)):
                    return name
    return None


def find_patterns(query, bindings=None) -> tuple[str, ...]:
    """Match restricted, checked structures; a declared P2 pairing is checked in the DB before timing."""
    validate_query(query)
    paper_pattern = (bindings or {}).get('paper_pattern')
    if paper_pattern in {'S6', 'S8'}:
        if ((bindings or {}).get('pattern_catalog', 'paper-v1') != 'paper-v1'
                or (bindings or {}).get('legacy_pattern', paper_pattern) != paper_pattern):
            return ()
        requested = bindings.get('mutation_site')
        if requested is not None and not isinstance(requested, (tuple, list)):
            return ()
        for site in find_mutation_sites(query, paper_pattern, bindings.get('form')):
            if requested is not None and tuple(requested) != site.address:
                continue
            try:
                verify_short_circuit_witness(query, site, bindings)
            except QueryError:
                continue
            return (paper_pattern,)
        return ()
    p3 = ('P3',) if find_p3_sites(query) else ()
    if type(query).__name__ == 'RowLimit':
        return ()
    has_with = isinstance(query, Query) and any(isinstance(part, With) for part in clauses(query))
    scor = None if has_with else _scor_pattern(query, bindings)
    if isinstance(query, ConditionalQuery):
        return (*p3, *((scor,) if scor else ()))
    if isinstance(query, UnionAll):
        p14 = ('P14',) if all(isinstance(branch, Query) and branch.limit is None
                             and all(_pure(item) for item in branch.returns)
                             and all(_pure(part) for part in clauses(branch))
                             for branch in query.branches) else ()
        return (*p3, *p14)
    # Legacy whole-clause swaps do not cross a newly introduced scope boundary.
    if has_with:
        return p3
    # Reordering an unordered LIMIT can choose different rows. S5/S6 have
    # separate empty-result/constant-projection contracts; legacy swaps do not.
    if query.limit is not None:
        return (scor,) if scor else ()
    parts = clauses(query)
    if not all(_pure(part.where) for part in parts) or not all(_pure(item) for item in query.returns):
        return ()
    matches = list(p3)
    edge = parts[0].pattern if parts else None
    if isinstance(edge, Edge):
        rel = edge.relationship
        if edge.direction == 'undirected' and rel.min_hops is None:
            matches.append('P1')
        inverse = _inverse_types(bindings)
        if (inverse and edge.direction in {'out', 'in'} and rel.type in inverse
                and not rel.alternatives and rel.min_hops is None
                and rel.variable is None and not rel.properties):
            matches.append('P2')
        if rel.alternatives and len(set((rel.type, *rel.alternatives))) > 1:
            matches.append('P8')
    if parts:
        predicate = parts[0].where
        if isinstance(predicate, Boolean) and len(predicate.terms) == 2:
            matches.append({'AND': 'P4', 'OR': 'P5'}[predicate.operator])
        if (isinstance(predicate, Comparison) and predicate.operator in {'=', '<>', '<', '<=', '>', '>='}
                and ((isinstance(predicate.left, Property) and isinstance(predicate.right, Literal))
                     or (isinstance(predicate.right, Property) and isinstance(predicate.left, Literal)))):
            matches.append('P6')
        if _label_site(edge) is not None:
            matches.append('P7')
    if _movable_predicate(parts):
        matches.append('P9')
    if (len(parts) == 2 and not any(part.optional for part in parts)
            and not (_variables(parts[0]) & _variables(parts[1]))
            and all(_local_predicate(part) for part in parts)):
        matches.append('P10')
    if _independent_branches(parts, False):
        matches.append('P11')
    if _chain(parts):
        matches.append('P12')
    if _independent_branches(parts, True):
        matches.append('P13')
    if _return_site(query, Case) is not None:
        matches.append('P15')
    site = _return_site(query, All)
    if site is not None and isinstance(query.returns[site].expression.items, Concat):
        matches.append('P16')
    if scor:
        matches.append(scor)
    return tuple(sorted(matches, key=lambda item: (item[0], int(item[1:]))))


def _plan_mutation(query, site, *, replacement=None, inverse_types=None):
    """Rewrite a typed candidate term; graph premises are deliberately unchecked.

    Algorithm 1 uses this same local rewrite kernel to solve one shared state.
    Only ``mutate`` can publish a pair after checking its state obligations.
    """
    from dependencies import _located, _clause_addresses

    # Occurrences are traced by their structural origin, including when one
    # immutable Node object was reused in independent lexical scopes. Rewrites
    # transport these origins in exactly the same branch that changes the AST.
    original_origins = {address: address for address, node in _located(query) if isinstance(node, Node)}
    origins = dict(original_origins)

    def transport(destination, source=None):
        for address in tuple(origins):
            if address[:len(destination)] == destination:
                del origins[address]
        if source is not None:
            origins.update({(*destination, *address[len(source):]): origin
                            for address, origin in original_origins.items() if address[:len(source)] == source})

    pattern = site.paper_pattern
    original = node_at(query, site.address)
    if pattern.startswith('S'):
        if replacement is None:
            raise QueryError('A short-circuit rewrite requires its replacement term')
        changed = replace_at(query, site.address, replacement)
        transport(site.address)
    elif pattern in {'P1', 'P3'}:
        changed = replace_at(query, site.address, replace(original, left=original.right, right=original.left))
        transport((*site.address, 'left'), (*site.address, 'right'))
        transport((*site.address, 'right'), (*site.address, 'left'))
    elif pattern == 'P2':
        if not isinstance(inverse_types, (tuple, list)) or len(inverse_types) != 2 or inverse_types[0] == inverse_types[1]:
            raise QueryError('An inverse rewrite requires two distinct relationship types')
        forward, reverse = inverse_types
        if original.relationship.type not in inverse_types:
            raise QueryError('The selected edge is outside the inverse relationship pairing')
        changed = replace_at(query, site.address, replace(original,
            direction={'out': 'in', 'in': 'out'}[original.direction],
            relationship=replace(original.relationship, type=reverse if original.relationship.type == forward else forward)))
    elif pattern in {'P4', 'P5'}:
        changed = replace_at(query, site.address, replace(original, terms=tuple(reversed(original.terms))))
        for index in range(len(original.terms)):
            transport((*site.address, 'terms', index), (*site.address, 'terms', len(original.terms) - index - 1))
    elif pattern == 'P6':
        changed = replace_at(query, site.address, replace(original, left=original.right, right=original.left))
        transport((*site.address, 'left'), (*site.address, 'right'))
        transport((*site.address, 'right'), (*site.address, 'left'))
    elif pattern == 'P7':
        changed = replace_at(query, site.address, replace(original, labels=tuple(reversed(original.labels))))
    elif pattern == 'P8':
        types = tuple(reversed((original.type, *original.alternatives)))
        changed = replace_at(query, site.address, replace(original, type=types[0], alternatives=types[1:]))
    elif pattern in {'P9', 'P10', 'P11'}:
        location = tuple(site.guards['query_address'])
        owner = node_at(query, location)
        parts = list(clauses(owner))
        first, second = site.guards['clause_indices']
        addresses = [address for _, address in _clause_addresses(owner, location)]
        if pattern == 'P9':
            parts[first], parts[second] = replace(parts[first], where=parts[second].where), replace(parts[second], where=parts[first].where)
        else:
            parts[first], parts[second] = parts[second], parts[first]
        changed = replace_at(query, location, with_clauses(owner, tuple(parts)))
        moved_fields = ('where',) if pattern == 'P9' else ('pattern', 'where')
        for field in moved_fields:
            transport((*addresses[first], field), (*addresses[second], field))
            transport((*addresses[second], field), (*addresses[first], field))
    elif pattern == 'P12':
        changed = replace_at(query, site.address, replace(original, branches=tuple(reversed(original.branches))))
        for index in range(len(original.branches)):
            transport((*site.address, 'branches', index), (*site.address, 'branches', len(original.branches) - index - 1))
    elif pattern == 'P13':
        changed = replace_at(query, site.address, Case(Negation(Call('coalesce', (original.condition, Literal(False)))),
                                                     original.otherwise, original.then))
        transport((*site.address, 'condition'))
        transport((*site.address, 'condition', 'expression', 'args', 0), (*site.address, 'condition'))
        transport((*site.address, 'then'), (*site.address, 'otherwise'))
        transport((*site.address, 'otherwise'), (*site.address, 'then'))
    else:
        raise QueryError(f'Unsupported paper rewrite {pattern}')
    validate_query(changed)
    if changed == query:
        raise QueryError('Mutation would not change the query structure')
    target_nodes = {address for address, node in _located(changed) if isinstance(node, Node)}
    if not set(origins).issubset(target_nodes):
        raise QueryError('A rewrite produced an invalid node-origin trace')
    return changed, origins


def plan_mutation(query, site, *, replacement=None, inverse_types=None):
    """Plan the local typed rewrite without publishing unchecked graph premises."""
    return _plan_mutation(query, site, replacement=replacement, inverse_types=inverse_types)[0]


def plan_mutation_with_correspondence(query, site, *, replacement=None, inverse_types=None):
    """Plan one rewrite and retain the unchanged/moved node occurrence origins.

    These equalities align the source and candidate copy without inspecting
    schema names or guessing from structurally similar atoms. Replacement-local
    nodes are fresh origins; imported bindings are resolved by dependency scope.
    """
    from population import BindingRelation

    changed, origins = _plan_mutation(query, site, replacement=replacement, inverse_types=inverse_types)
    links = tuple(BindingRelation(('query', *source), ('alternatives', 0, *target))
                  for target, source in sorted(origins.items(), key=lambda item: tuple(map(str, item[0]))))
    return changed, links


def _paper_mutation(query, legacy_pattern, bindings):
    """Explicit catalog bridge; historical callers retain historical numbering."""
    paper_pattern, form = bindings['paper_pattern'], bindings.get('form')
    requested = bindings.get('mutation_site')
    if requested is not None and (not isinstance(requested, (tuple, list))
                                 or any(not (isinstance(part, str) or type(part) is int) for part in requested)):
        raise QueryError('mutation_site requires a structural address')
    site = (select_mutation_site(query, paper_pattern, form, requested) if requested is not None
            else next(iter(find_mutation_sites(query, paper_pattern, form)), None))
    if site is None:
        raise QueryError(f'mutation_site is not an applicable paper {paper_pattern} location')
    from paper_patterns import compositional_rule
    expected_legacy = compositional_rule(paper_pattern, site.form).legacy_pattern
    if (legacy_pattern != expected_legacy or bindings.get('legacy_pattern', legacy_pattern) != expected_legacy
            or bindings.get('pattern_catalog', 'paper-v1') != 'paper-v1'):
        raise QueryError('Explicit paper catalog and legacy pattern mapping disagree')
    replacement = None
    inverse_types = None
    if paper_pattern.startswith('S'):
        from dependencies import _constant_columns, _streamable
        from ir import from_data, decode_fragment, row_stream_schema, RowStream, Expression, Pattern
        if paper_pattern in {'S6', 'S8'}:
            verify_short_circuit_witness(query, site, bindings)
        else:
            verify_rule_premises(query, site, bindings)
        try:
            payload = bindings['replacement']
            replacement = decode_fragment(payload) if isinstance(payload, dict) and 'fragment' in payload else from_data(payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise QueryError(f'Invalid replacement row stream: {exc}') from exc
        original_fragment = node_at(query, site.address)
        if isinstance(original_fragment, RowStream):
            if not isinstance(replacement, RowStream):
                raise QueryError('A row-stream site requires a row-stream replacement')
            environment = site_environment(query, site.address)
            if row_stream_schema(replacement, environment) != row_stream_schema(original_fragment, environment):
                raise QueryError('A short-circuit replacement must preserve the branch output schema')
        elif isinstance(original_fragment, Match):
            if not isinstance(replacement, Match) or replacement.optional:
                raise QueryError('An empty mandatory input requires a mandatory downstream MATCH')
            if paper_pattern == 'S9':
                owner_address = tuple(site.guards['envelope_site'])
                owner = node_at(query, owner_address)
                parts = clauses(owner)
                first_names = set(pattern_variables(parts[0].pattern))
                if first_names & set(pattern_variables(replacement.pattern)):
                    raise QueryError('The downstream replacement must remain independent of the empty input')
        elif isinstance(original_fragment, Pattern):
            if not isinstance(replacement, Pattern):
                raise QueryError('A matching site requires a matching-pattern replacement')
        elif isinstance(original_fragment, Expression):
            if not isinstance(replacement, Expression):
                raise QueryError('A scalar site requires a scalar replacement')
            environment = site_environment(query, site.address)
            def scalar_schema(expression):
                return row_stream_schema(Query(None, None, (Alias(expression, 'value'),)), environment)
            old_kind = scalar_schema(original_fragment)[0][1].rstrip('?')
            new_kind = scalar_schema(replacement)[0][1].rstrip('?')
            compatible = (new_kind in {'boolean', 'null'} if paper_pattern in {'S1', 'S2'} else
                          old_kind == new_kind or 'null' in {old_kind, new_kind})
            if not compatible:
                raise QueryError('A scalar replacement must preserve its inferred output schema')
        else:
            raise QueryError('Unsupported replacement interface')
        if not __import__('dependencies')._pure(replacement):
            raise QueryError('A replacement must be a deterministic total read-only fragment')
        if paper_pattern in {'S6', 'S8'} and not _streamable(replacement):
            raise QueryError('A short-circuit replacement must remain streamable')
        if paper_pattern == 'S6':
            original = node_at(query, tuple(site.guards['union_site']))
            if _constant_columns(replacement) != _constant_columns(original):
                raise QueryError('S6 must preserve every constant projection')
        if paper_pattern == 'S10':
            changed_membership = plan_mutation(query, site, replacement=replacement)
            if not any(item.address == site.address for item in find_mutation_sites(changed_membership, 'S10', site.form)):
                raise QueryError('A membership replacement must retain its null-safe predicate and DISTINCT envelope')
    elif paper_pattern == 'P2':
        inverse_types = tuple(verify_inverse_pair(query, site, bindings)['inverse_types'])
    from dependencies import propagate_equivalence
    propagate_equivalence(query, site, bindings)
    return plan_mutation(query, site, replacement=replacement, inverse_types=inverse_types)


def mutate(query, pattern: str, *, bindings=None):
    if bindings is not None and 'paper_pattern' in bindings:
        return _paper_mutation(query, pattern, bindings)
    if pattern not in find_patterns(query, bindings):
        raise QueryError(f'Pattern {pattern!r} does not apply to this query')
    if pattern == 'P3':
        sites = find_p3_sites(query)
        requested = (bindings or {}).get('mutation_site')
        if requested is None:
            site = sites[0]
        else:
            if (not isinstance(requested, (tuple, list))
                    or any(not (isinstance(part, str) or type(part) is int) for part in requested)):
                raise QueryError('mutation_site requires a structural address')
            site = next((candidate for candidate in sites if candidate.address == tuple(requested)), None)
            if site is None:
                raise QueryError('mutation_site is not an applicable P3 location')
        edge = node_at(query, site.address)
        result = replace_at(query, site.address, replace(edge, left=edge.right, right=edge.left))
    elif pattern in SCOR_PAPER_PATTERNS:
        spec = bindings['scor']
        result = _replace_high(query, spec['high_type'], spec['reference_type'])
    elif pattern == 'P14':
        result = replace(query, branches=tuple(reversed(query.branches)))
    elif pattern in {'P15', 'P16'}:
        kind = Case if pattern == 'P15' else All
        index = _return_site(query, kind)
        projection = query.returns[index]
        expression = projection.expression
        if pattern == 'P15':
            # NULL follows ELSE in Cypher CASE; NOT NULL alone would lose that case.
            complement = Negation(Call('coalesce', (expression.condition, Literal(False))))
            changed = Case(complement, expression.otherwise, expression.then)
        else:
            groups = expression.items
            changed = replace(expression, items=Concat(groups.right, groups.left))
        returns = list(query.returns)
        returns[index] = replace(projection, expression=changed)
        result = replace(query, returns=tuple(returns))
    else:
        parts = list(clauses(query))
        first = parts[0]
        if pattern == 'P1':
            edge = first.pattern
            parts[0] = replace(first, pattern=replace(edge, left=edge.right, right=edge.left))
        elif pattern == 'P2':
            edge = first.pattern
            forward, reverse = _inverse_types(bindings)
            relation_type = reverse if edge.relationship.type == forward else forward
            edge = replace(edge, direction={'out': 'in', 'in': 'out'}[edge.direction],
                           relationship=replace(edge.relationship, type=relation_type))
            parts[0] = replace(first, pattern=edge)
        elif pattern in {'P4', 'P5'}:
            parts[0] = replace(first, where=replace(first.where, terms=tuple(reversed(first.where.terms))))
        elif pattern == 'P6':
            predicate = first.where
            dual = {'=': '=', '<>': '<>', '<': '>', '<=': '>=', '>': '<', '>=': '<='}
            parts[0] = replace(first, where=Comparison(predicate.right, dual[predicate.operator], predicate.left))
        elif pattern == 'P7':
            node_index = _label_site(first.pattern)
            if isinstance(first.pattern, Node):
                changed = replace(first.pattern, labels=tuple(reversed(first.pattern.labels)))
            else:
                field = 'left' if node_index == 0 else 'right'
                node = getattr(first.pattern, field)
                changed = replace(first.pattern, **{field: replace(node, labels=tuple(reversed(node.labels)))})
            parts[0] = replace(first, pattern=changed)
        elif pattern == 'P8':
            edge = first.pattern
            types = tuple(reversed((edge.relationship.type, *edge.relationship.alternatives)))
            relation = replace(edge.relationship, type=types[0], alternatives=types[1:])
            parts[0] = replace(first, pattern=replace(edge, relationship=relation))
        elif pattern == 'P9':
            parts[0], parts[1] = replace(parts[0], where=parts[1].where), replace(parts[1], where=parts[0].where)
        elif pattern in {'P10', 'P12'}:
            parts.reverse()
        elif pattern in {'P11', 'P13'}:
            parts[1], parts[2] = parts[2], parts[1]
        result = with_clauses(query, tuple(parts))
    validate_query(result)
    if result == query:
        raise QueryError('Mutation would not change the query structure')
    return result
