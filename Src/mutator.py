"""Symmetric rewrites and graph-specific SCor reference substitutions."""

from dataclasses import fields, is_dataclass, replace

from query import (
    Alias, All, Boolean, Call, Case, Comparison, Concat, ConditionalQuery, Count, Edge, Exists,
    ListExpr, Literal, Negation, Node, Property, QueryError,
    UnionAll, Variable, clauses, expression_variables, pattern_variables,
    validate_query, with_clauses,
)


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
        return () if expression.where is None else (expression.where,)
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
        if expression.name.lower() not in {'length', 'coalesce', 'range', 'size'}:
            return False
        if expression.name.lower() == 'range':
            return (len(expression.args) in (2, 3)
                    and all(isinstance(arg, Literal) and type(arg.value) is int for arg in expression.args)
                    and (len(expression.args) == 2 or expression.args[2].value != 0))
    if not isinstance(expression, (Comparison, Boolean, Negation, Exists, Count, Call, Alias, Case, ListExpr, Concat, All)):
        return False
    return all(_pure(child) for child in _children(expression))


def _length_only(expression, path):
    if isinstance(expression, Call) and expression.name.lower() == 'length' and expression.args == (Variable(path),):
        return True
    if isinstance(expression, Variable) and expression.name == path:
        return False
    if isinstance(expression, Property) and expression.variable == path:
        return False
    return all(_length_only(child, path) for child in _children(expression))


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


def _constant_truth(expression):
    """Recognize only the tiny, checked constant pool used by our templates.

    Distinguish an unknown expression from a known NULL. Arbitrary properties,
    subqueries and functions cannot certify a decisive guard.
    """
    if isinstance(expression, Literal) and type(expression.value) in (bool, type(None)):
        return True, expression.value
    if isinstance(expression, Negation):
        known, value = _constant_truth(expression.expression)
        return (True, None if value is None else not value) if known else (False, None)
    if isinstance(expression, Comparison) and isinstance(expression.left, Literal) and isinstance(expression.right, Literal):
        left, right = expression.left.value, expression.right.value
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
            and node.properties == ((spec.get('root_key', 'id'), Literal(spec.get('root_value', 1))),))


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
    if name == 'S3':
        if not isinstance(query, ConditionalQuery):
            return None
        known, condition = _constant_truth(query.condition)
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
                    if _constant_is(cheap, absorbing) and _only_high_inside(query, (expensive,), high):
                        return name
    elif name == 'S4':
        for expression in projections:
            if isinstance(expression, Case):
                known, condition = _constant_truth(expression.condition)
                if known:
                    skipped = expression.otherwise if condition is True else expression.then
                    if _only_high_inside(query, (skipped,), high):
                        return name
    elif name == 'S5':
        if query.limit == 0 or any(not part.optional and _constant_is(part.where, False) for part in parts):
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
                    if not isinstance(argument, Literal):
                        break
                    if argument.value is not None:
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
                and _constant_is(parts[0].where, False) and not _high_count(parts[0], high)):
            return name
    elif name == 'S10':
        if not (query.distinct and len(parts) == 1 and isinstance(parts[0].pattern, Node)):
            return None
        predicate = parts[0].where
        if isinstance(predicate, Boolean) and predicate.operator == 'AND' and len(predicate.terms) == 2:
            for guard, membership in (predicate.terms, tuple(reversed(predicate.terms))):
                exists = membership.expression if isinstance(membership, Negation) else membership
                if not _constant_is(guard, False) or not isinstance(exists, Exists) or not isinstance(exists.pattern, Edge):
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
    scor = _scor_pattern(query, bindings)
    if isinstance(query, ConditionalQuery):
        return (scor,) if scor else ()
    if isinstance(query, UnionAll):
        return ('P14',) if all(branch.limit is None and all(_pure(item) for item in branch.returns)
                              and all(_pure(part.where) for part in clauses(branch))
                              for branch in query.branches) else ()
    # Reordering an unordered LIMIT can choose different rows. S5/S6 have
    # separate empty-result/constant-projection contracts; legacy swaps do not.
    if query.limit is not None:
        return (scor,) if scor else ()
    parts = clauses(query)
    if not all(_pure(part.where) for part in parts) or not all(_pure(item) for item in query.returns):
        return ()
    matches = []
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
        if (edge.direction == 'undirected' and rel.min_hops is not None and rel.max_hops is not None
                and (edge.path_variable is None or
                     all(_length_only(expr, edge.path_variable)
                         for expr in (*query.returns, *(part.where for part in parts))))):
            matches.append('P3')
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


def mutate(query, pattern: str, *, bindings=None):
    if pattern not in find_patterns(query, bindings):
        raise QueryError(f'Pattern {pattern!r} does not apply to this query')
    if pattern in SCOR_PAPER_PATTERNS:
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
        if pattern in {'P1', 'P3'}:
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
