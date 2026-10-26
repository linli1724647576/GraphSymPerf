"""Bounded-path atoms lowered to endpoint atoms, then lifted to tree paths.

The fixed-atom engine solves the common binding graph, scalar and quantity
goals.  This operator backend supplies a checked path/edge bijection and a
FALSE-prefix growth certificate; it never selects a test-pattern identifier.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from itertools import product
import json

import ir
from dependencies import (analyze_dependencies, entity_symbol_origin, node_at,
                          matching_quantity_observation, scalar_value_observation,
                          entity_binding_observation, premise_observation,
                          _matching_context, _row_fact, _close_row_fact, replace_at)
from population import (PopulationError, PopulationResult, PathDemand, MatchDemand,
                        ValueDemand, EntityBindingDemand, BindingRelation, _located,
                        _symbol_lookup, _address_id, _scalar, _typed, _bag,
                        _compare, _affine, _Interpreter)
from state import GraphSpec, render_state_checks


def _bounded(value):
    return isinstance(value, ir.Edge) and value.relationship.max_hops is not None


def _origin(analysis, symbols, sid):
    records = {record['symbol_id']: record for record in analysis.symbols}
    seen = set()
    while sid in records:
        if sid in seen:
            raise PopulationError('A path alias has a cyclic producer')
        seen.add(sid)
        record = records[sid]
        prior = symbols.get(record.get('producer_node'))
        if prior is None or records.get(prior, {}).get('kind') != record['kind']:
            return sid
        sid = prior
    raise PopulationError('A path observation has no actual lexical producer')


def _values(predicates, fixed):
    keys = sorted({part.key for predicate in predicates for _, part in _located(predicate)
                   if isinstance(part, ir.Property)} - set(fixed))
    if len(keys) > 8:
        raise PopulationError('Path local-property search is limited to eight properties')
    domains = {key: [True, False, None] for key in keys}
    for predicate in predicates:
        for _, part in _located(predicate):
            if not isinstance(part, ir.Comparison):
                continue
            for prop, literal in ((part.left, part.right), (part.right, part.left)):
                if not isinstance(prop, ir.Property) or not isinstance(literal, ir.Literal) or prop.key not in domains:
                    continue
                value = literal.value
                candidates = [value]
                if type(value) is int:
                    candidates += [item for item in (value - 1, value + 1) if -(2**63) <= item < 2**63]
                elif type(value) is float:
                    candidates += [value - 1, value + 1]
                elif type(value) is str:
                    candidates.append(value + '_other')
                for candidate in reversed(candidates):
                    if _typed(candidate) not in {_typed(item) for item in domains[prop.key]}:
                        domains[prop.key].insert(0, candidate)
    for values in product(*(domains[key] for key in keys)):
        yield fixed | dict(zip(keys, values))


def _constant_properties(node):
    return {key: literal.value for key, literal in node.properties}


def _rewrite_local(value, binder, endpoint):
    if isinstance(value, ir.Property):
        if value.variable != binder:
            raise PopulationError('Path ALL must be node-local')
        return replace(value, variable=endpoint)
    if is_dataclass(value):
        return replace(value, **{field.name: _rewrite_local(getattr(value, field.name), binder, endpoint)
                                 for field in fields(value)})
    if isinstance(value, tuple):
        return tuple(_rewrite_local(item, binder, endpoint) for item in value)
    return value


def _uid_base(value):
    return value['affine']['base'] if isinstance(value, dict) else value


def _value_at(value, index=0):
    return value['affine']['base'] + value['affine']['step'] * index if isinstance(value, dict) else value


class _PathModel:
    def __init__(self, source, alternatives, demands, obligations, correspondence):
        self.trees = [(('query',), source), *((('alternatives', i), tree) for i, tree in enumerate(alternatives))]
        self.demands, self.obligations = tuple(demands), tuple(obligations)
        self.paths, self.infos, self.roles, self.quantifiers = {}, {}, {}, {}
        self.links = tuple(correspondence or ())
        for prefix, tree in self.trees:
            ir.validate_query(tree)
            located = list(_located(tree))
            paths = [(address, value) for address, value in located if _bounded(value)]
            if len(paths) != 1:
                raise PopulationError('Path lifting currently requires one lexical bounded-path occurrence per query copy')
            address, path = paths[0]
            rel = path.relationship
            if (path.direction != 'undirected' or rel.path_mode not in ('trail', 'acyclic')
                    or rel.variable is not None or not rel.type or rel.alternatives
                    or not path.path_variable or type(rel.min_hops) is not int
                    or not 1 <= rel.min_hops <= rel.max_hops <= 8):
                raise PopulationError('Path lifting requires a typed undirected TRAIL/ACYCLIC path with bounds in 1..8')
            analysis = analyze_dependencies(tree)
            symbols = _symbol_lookup(analysis)
            symbols.update({use['node_id']: use['symbol_id'] for use in analysis.uses})
            sid = symbols.get(_address_id((*address, 'path_variable')))
            if sid is None:
                raise PopulationError('The bounded path has no actual lexical binding')
            info = {'prefix': prefix, 'tree': tree, 'located': located, 'lookup': dict(located),
                    'analysis': analysis, 'symbols': symbols, 'path_sid': sid}
            self.infos[prefix], self.paths[prefix] = info, (address, path)
            quantifiers = {}
            for location, value in located:
                if isinstance(value, ir.All):
                    if not (isinstance(value.items, ir.Call) and value.items.name == 'nodes'
                            and len(value.items.args) == 1 and isinstance(value.items.args[0], ir.Variable)):
                        raise PopulationError('Path lifting requires ALL over nodes of the bound path')
                    used = symbols.get(_address_id((*location, 'items', 'args', 0)))
                    if _origin(analysis, symbols, used) != sid:
                        raise PopulationError('An ALL observation refers to a different path producer')
                    quantifiers[location] = value
                    if any(isinstance(part, ir.Property) and part.key == 'uid'
                           for _, part in _located(value.predicate)):
                        raise PopulationError('UID-dependent ALL predicates need an affine path-value proof')
            if not quantifiers:
                raise PopulationError('A lifted path requires at least one node-local ALL observation')
            self.quantifiers[prefix] = quantifiers
            _, _, parts, index = _matching_context(tree, address)
            if parts[index][0].optional:
                raise PopulationError('OPTIONAL bounded paths need a separate NULL-path transfer rule')
        source_address, source_path = self.paths[('query',)]
        self.source_path = source_path
        self.relationship_type = source_path.relationship.type
        self._roles(source_address)
        root_nodes, target_nodes = [], []
        for prefix, info in self.infos.items():
            for address, value in info['located']:
                if not isinstance(value, ir.Node):
                    continue
                role = self.roles.get((*prefix, *address))
                if role == 'root':
                    root_nodes.append(value)
                elif role == 'target':
                    target_nodes.append(value)
        self.root_labels = set(label for node in root_nodes for label in node.labels)
        self.target_labels = set(label for node in target_nodes for label in node.labels)
        if not self.root_labels or not self.target_labels or self.root_labels & self.target_labels:
            raise PopulationError('Path tree lifting needs disjoint nonempty anchor and endpoint labels')
        root_fixed = {}
        for node in root_nodes:
            for key, value in _constant_properties(node).items():
                if key in root_fixed and _compare(root_fixed[key], '=', value) is not True:
                    raise PopulationError('The path anchor has conflicting fixed properties')
                root_fixed[key] = value
        self.root_fixed = root_fixed
        self.predicates = [value.predicate for quantifiers in self.quantifiers.values() for value in quantifiers.values()]
        try:
            self.true_values = next(values for values in _values(self.predicates, root_fixed)
                                    if all(_scalar(predicate, values) is True for predicate in self.predicates))
        except StopIteration as error:
            raise PopulationError('The current path family needs a common TRUE anchor/interior valuation') from error
        self.length = self._length()
        self._closed_reads()

    def _roles(self, source_address):
        # The source definition and explicit copy correspondence determine
        # physical endpoint roles; printed variable names do not align copies.
        endpoint_roles = {('query', *source_address, 'left'): 'root',
                          ('query', *source_address, 'right'): 'target'}
        for _ in range(len(self.links) + 1):
            changed = False
            for link in self.links:
                if not isinstance(link, BindingRelation) or not link.equal:
                    continue
                left, right = tuple(link.left_address), tuple(link.right_address)
                if left in endpoint_roles and right not in endpoint_roles:
                    endpoint_roles[right] = endpoint_roles[left]
                    changed = True
                if right in endpoint_roles and left not in endpoint_roles:
                    endpoint_roles[left] = endpoint_roles[right]
                    changed = True
            if not changed:
                break
        source = self.paths[('query',)][1]
        for prefix, (address, path) in self.paths.items():
            if path.relationship != source.relationship:
                raise PopulationError('Copied path atoms must retain their actual relationship and bounds')
            for side in ('left', 'right'):
                forest = (*prefix, *address, side)
                if forest not in endpoint_roles:
                    # Legacy callers without origin records must still have
                    # unambiguous concrete endpoint constraints.
                    node = getattr(path, side)
                    candidates = [role for candidate, role in ((source.left, 'root'), (source.right, 'target'))
                                  if set(node.labels) == set(candidate.labels)
                                  and _typed(_constant_properties(node)) == _typed(_constant_properties(candidate))]
                    if len(candidates) != 1:
                        raise PopulationError('A path copy needs explicit unambiguous endpoint correspondence')
                    endpoint_roles[forest] = candidates[0]
            info = self.infos[prefix]
            role_symbols = {}
            for side in ('left', 'right'):
                sid = info['symbols'][_address_id((*address, side, 'variable'))]
                role_symbols[entity_symbol_origin(info['analysis'], sid)['symbol_id']] = endpoint_roles[(*prefix, *address, side)]
            for location, node in info['located']:
                if not isinstance(node, ir.Node):
                    continue
                sid = info['symbols'].get(_address_id((*location, 'variable')))
                origin = entity_symbol_origin(info['analysis'], sid)['symbol_id']
                if origin in role_symbols:
                    self.roles[(*prefix, *location)] = role_symbols[origin]

    def _length(self):
        low = max(path.relationship.min_hops for _, path in self.paths.values())
        high = min(path.relationship.max_hops for _, path in self.paths.values())
        for demand in self.demands:
            if not isinstance(demand, PathDemand):
                continue
            if tuple(demand.address) not in {(*prefix, *address) for prefix, (address, _) in self.paths.items()}:
                raise PopulationError('A path demand must address an actual bounded atom')
            if demand.min_length is not None:
                low = max(low, demand.min_length)
            if demand.max_length is not None:
                high = min(high, demand.max_length)
            for address, truth in demand.all_values:
                prefix = ('query',) if tuple(address)[:1] == ('query',) else ('alternatives', tuple(address)[1])
                if tuple(address)[len(prefix):] not in self.quantifiers.get(prefix, {}):
                    raise PopulationError('A path truth demand must address its actual ALL observation')
                if truth is not None and type(truth) is not bool:
                    raise PopulationError('Path truth values must be TRUE/FALSE/NULL')
        if low > high:
            raise PopulationError('The bounded path length demands have an empty intersection')
        return low

    def _closed_reads(self):
        for prefix, info in self.infos.items():
            address, path = self.paths[prefix]
            for location, value in info['located']:
                if isinstance(value, ir.Edge) and location != address:
                    if self.relationship_type in ((value.relationship.type,) + value.relationship.alternatives):
                        raise PopulationError('An additional edge read can observe lifted path relationships')
                if not isinstance(value, ir.Node) or self.roles.get((*prefix, *location)) == 'root':
                    continue
                if location in ((*address, 'left'), (*address, 'right')):
                    continue
                parent = info['lookup'].get(location[:-1])
                if isinstance(parent, ir.Edge):
                    continue
                if not value.labels or set(value.labels).issubset(self.target_labels):
                    raise PopulationError('An unprotected node scan can observe the lifted path population')

    def lower(self, prefix):
        info = self.infos[prefix]
        address, path = self.paths[prefix]
        target = path.left.variable if self.roles[(*prefix, *address, 'left')] == 'target' else path.right.variable
        def visit(value, location=()):
            if location == address:
                return replace(value, relationship=replace(value.relationship, min_hops=None, max_hops=None),
                               path_variable=None, left=visit(value.left, (*location, 'left')),
                               right=visit(value.right, (*location, 'right')))
            if location in self.quantifiers[prefix]:
                return _rewrite_local(value.predicate, value.variable, target)
            if isinstance(value, ir.Call) and value.name == 'length':
                if len(value.args) != 1 or not isinstance(value.args[0], ir.Variable):
                    raise PopulationError('A path length must observe the actual bound path')
                sid = info['symbols'].get(_address_id((*location, 'args', 0)))
                if _origin(info['analysis'], info['symbols'], sid) != info['path_sid']:
                    raise PopulationError('A length observation refers to a different producer')
                return ir.Literal(self.length)
            same_keyed_anchor = (isinstance(value, ir.Node) and bool(value.properties)
                and bool(set(value.labels) & self.root_labels)
                and all(key in self.root_fixed and _compare(self.root_fixed[key], '=', literal.value) is True
                        for key, literal in value.properties))
            if isinstance(value, ir.Node) and (self.roles.get((*prefix, *location)) == 'root' or same_keyed_anchor):
                props = _constant_properties(value)
                props.update(self.true_values)
                return replace(value, properties=tuple((key, ir.Literal(item)) for key, item in props.items()))
            if is_dataclass(value):
                return replace(value, **{field.name: visit(getattr(value, field.name), (*location, field.name))
                                         for field in fields(value)})
            if isinstance(value, tuple):
                return tuple(visit(item, (*location, i)) for i, item in enumerate(value))
            return value
        return visit(info['tree'])

    def lower_demands(self):
        result = []
        for demand in self.demands:
            if isinstance(demand, PathDemand):
                continue
            if not isinstance(demand, MatchDemand):
                raise PopulationError('Unknown bounded-path population demand')
            if demand.candidate_kind == 'path_false_prefix':
                address = tuple(demand.address)
                kind = 'left_scalar' if self.roles.get((*address, 'left')) == 'target' else 'right_scalar'
                demand = replace(demand, candidate_kind=kind)
            result.append(demand)
        return tuple(result)

    def lift(self, spec):
        nodes = json.loads(json.dumps(list(spec.node_groups)))
        edges = []
        next_uid = max(_value_at(group['properties']['uid'], group['count'] - 1) for group in nodes) + 1
        occupied = {label for group in nodes for label in group['labels']}
        occupied.update(label for info in self.infos.values() for _, value in info['located']
                        if isinstance(value, ir.Node) for label in value.labels)
        interior = 'GspPathInterior'
        while interior in occupied:
            interior += '_'
        certificates = []
        def family(uid):
            base = _uid_base(uid)
            candidates = [group for group in nodes if _uid_base(group['properties']['uid']) <= base
                          <= _value_at(group['properties']['uid'], group['count'] - 1)]
            if len(candidates) != 1:
                raise PopulationError('A lifted path endpoint has no unambiguous node family')
            return candidates[0]
        for original in spec.edge_groups:
            if original['type'] != self.relationship_type:
                edges.append(dict(original))
                continue
            group = dict(original)
            left, right = family(group['source']), family(group['target'])
            if set(left['labels']) & self.root_labels and set(right['labels']) & self.target_labels:
                pass
            elif set(right['labels']) & self.root_labels and set(left['labels']) & self.target_labels:
                group['source'], group['target'] = group['target'], group['source']
                group['source_label'], group['target_label'] = group['target_label'], group['source_label']
                left, right = right, left
            else:
                raise PopulationError('A lifted relationship does not connect the certified path endpoints')
            endpoint_props = {key: value for key, value in right['properties'].items() if key != 'uid'}
            if any(isinstance(value, dict) for value in endpoint_props.values()):
                raise PopulationError('Affine path scalar properties need a separate identity proof')
            truths = [_scalar(predicate, endpoint_props) for predicate in self.predicates]
            rejected = any(value is False for value in truths)
            shared = rejected and type(group['source']) is int and self.length >= 2
            previous, label = group['source'], group['source_label']
            if shared:
                # Every new path shares an actual FALSE node before its
                # variable suffix. The endpoint valuation stays unchanged,
                # so the fixed-atom growth exclusions remain valid.
                current = next_uid
                next_uid += 1
                nodes.append({'count': 1, 'labels': [interior], 'properties': endpoint_props | {'uid': current}})
                edges.append({**group, 'count': 1, 'source': previous, 'target': current,
                              'source_label': label, 'target_label': interior})
                previous, label, start_depth = current, interior, 2
            else:
                start_depth = 1
            for depth in range(start_depth, self.length):
                current = next_uid if group['count'] == 1 else _affine(next_uid)
                next_uid += group['count']
                nodes.append({'count': group['count'], 'labels': [interior],
                              'properties': self.true_values | {'uid': current}})
                edges.append({**group, 'source': previous, 'target': current,
                              'source_label': label, 'target_label': interior})
                previous, label = current, interior
            edges.append({**group, 'source': previous, 'source_label': label})
            certificates.append({'paths': group['count'], 'length': self.length,
                                 'all_values': truths, 'shared_false_prefix': shared,
                                 'failure_depth': 1 if shared else self.length if rejected else None,
                                 'tree_edges_are_unique': True})
        result = replace(spec, node_groups=tuple(nodes), edge_groups=tuple(edges))
        result._groups()
        return result, certificates


def synthesize(source, *, alternatives=(), scale, data_seed, demands=(), obligations=(),
               result_rows=None, correspondence=None, min_result_rows=0):
    model = _PathModel(source, tuple(alternatives), demands, obligations, correspondence)
    lowered = [model.lower(prefix) for prefix, _ in model.trees]
    from relational_population import synthesize as fixed_synthesize
    solved = fixed_synthesize(lowered[0], alternatives=tuple(lowered[1:]), scale=scale,
        data_seed=data_seed, demands=model.lower_demands(), obligations=obligations,
        result_rows=result_rows, correspondence=correspondence, min_result_rows=min_result_rows)
    proof, small_certificates = model.lift(GraphSpec.from_dict(solved.evidence['validation_graph_spec']))
    full, certificates = model.lift(solved.spec)
    if any(demand.candidate_kind == 'path_false_prefix' and demand.candidate_matches is not None
           and demand.candidate_matches > (demand.exact_matches or 0) for demand in demands if isinstance(demand, MatchDemand)):
        if model.length < 2 or not any(item['shared_false_prefix'] for item in certificates):
            raise PopulationError('Path work needs a proved shared FALSE prefix and a nonempty suffix')
    rows = _Interpreter(proof).run(source)
    if _bag(rows) != _bag(solved.expected_rows):
        raise PopulationError('Lifting a bounded path changed its complete source result bag')
    for _, tree in model.trees:
        if _bag(_Interpreter(proof).run(tree)) != _bag(rows):
            raise PopulationError('The lifted path query copies have different complete result bags')
        if full.counts()['nodes'] <= 5000 and _bag(_Interpreter(full).run(tree)) != _bag(rows):
            raise PopulationError('Full path growth changed a complete query result bag')
    from query import render_query
    checks = []
    def checked(expression, *, proof_expression=None, reason):
        query = ir.Query(None, None, (ir.Alias(expression, 'path_fact'),))
        proof_query = (query if proof_expression is None else
                       ir.Query(None, None, (ir.Alias(proof_expression, 'path_fact'),)))
        if _Interpreter(proof).run(proof_query) != [{'path_fact': True}]:
            raise PopulationError(reason)
        if full.counts()['nodes'] <= 5000 and _Interpreter(full).run(query) != [{'path_fact': True}]:
            raise PopulationError(reason + ' on the actual full graph')
        check = {'query': render_query(query), 'expected_rows': [{'path_fact': True}]}
        if check not in checks:
            checks.append(check)
    # Rebuild checks on the original path AST. Checks emitted for quotient
    # edges are not valid evidence about real bounded paths.
    for demand in demands:
        if not isinstance(demand, MatchDemand):
            continue
        address = tuple(demand.address)
        prefix = ('query',) if address[:1] == ('query',) else ('alternatives', address[1])
        local = address[len(prefix):]
        tree = model.infos[prefix]['tree']
        pattern = node_at(tree, local)
        role = 'left'
        if isinstance(pattern, ir.Edge) and model.roles.get((*prefix, *local, 'right')) == 'root':
            role = 'right'
        fact = matching_quantity_observation(tree, local, minimum=demand.min_matches,
            exact=demand.exact_matches, relationship_type=demand.relationship_type, logical_source_role=role)
        checked(fact['expression'], reason=f'An actual bounded-path matching quantity differs from the solved quantity at {address}')
        if _bounded(pattern):
            raw = replace_at(tree, (*local[:-1], 'where'), None)
            if demand.candidate_matches is not None:
                exact = max(demand.candidate_matches, demand.exact_matches or demand.min_matches)
                proof_exact = (demand.exact_matches or demand.min_matches) + min(
                    max(0, exact - (demand.exact_matches or demand.min_matches)), 2)
                raw_fact = matching_quantity_observation(raw, local, exact=exact, logical_source_role=role)
                proof_fact = matching_quantity_observation(raw, local, exact=proof_exact, logical_source_role=role)
                checked(raw_fact['expression'], proof_expression=proof_fact['expression'],
                        reason='The actual raw path candidate population differs from its work demand')
            wrong_length = replace_at(raw, (*local[:-1], 'where'), ir.Comparison(
                ir.Call('length', (ir.Variable(pattern.path_variable),)), '<>', ir.Literal(model.length)))
            length_fact = matching_quantity_observation(wrong_length, local, exact=0, logical_source_role=role)
            checked(length_fact['expression'], reason='An actual path violates its solved length')
    for demand in demands:
        if not isinstance(demand, PathDemand):
            continue
        address = tuple(demand.address)
        prefix = ('query',) if address[:1] == ('query',) else ('alternatives', address[1])
        local = address[len(prefix):]
        tree = model.infos[prefix]['tree']
        owner, _, parts, index = _matching_context(tree, local)
        pattern = node_at(tree, local)
        for truth_address, truth in demand.all_values:
            expression = node_at(tree, tuple(truth_address)[len(prefix):])
            # Observe the quantifier on real matching witnesses, after this
            # clause's filter. TRUE guards may coexist with failed raw paths;
            # a zero witness domain is vacuous, while explicit ValueDemand
            # observations below separately require a nonempty row domain.
            expression = replace(expression, items=ir.Call('nodes', (ir.Variable(pattern.path_variable),)))
            predicate = ir.Call('coalesce', (premise_observation(expression, 'equal', truth), ir.Literal(False)))
            observed = []
            fact = _close_row_fact(tree, owner, _row_fact(parts[:index + 1], predicate, nonempty=False),
                                   observed, nonempty=False)
            checked(fact, reason='A PathDemand TRUE/FALSE/NULL value conflicts with actual matching witnesses')
    for obligation in obligations:
        if not isinstance(obligation, (ValueDemand, EntityBindingDemand)):
            continue
        address = tuple(obligation.address)
        prefix = ('query',) if address[:1] == ('query',) else ('alternatives', address[1])
        if isinstance(obligation, EntityBindingDemand):
            fact = entity_binding_observation(model.infos[prefix]['tree'], address[len(prefix):], obligation.relation)
        else:
            fact = scalar_value_observation(model.infos[prefix]['tree'], address[len(prefix):],
                                            relation=obligation.relation, value=obligation.value)
        checked(fact['expression'], reason='An actual path scalar/binding fact differs from its solved value')
    source_address = model.paths[('query',)][0]
    source_demand = next((demand for demand in demands if isinstance(demand, MatchDemand)
                          and tuple(demand.address) == ('query', *source_address)), None)
    constraints = {**solved.constraints,
        'valid_paths': source_demand.exact_matches if source_demand is not None else None,
        'path_operator': {'length': model.length,
        'bounds': [model.source_path.relationship.min_hops, model.source_path.relationship.max_hops],
        'families': certificates, 'all_demands': [json.loads(json.dumps(demand.__dict__))
                                                for demand in demands if isinstance(demand, PathDemand)]}}
    evidence = {**solved.evidence, 'construction': 'path_operator_lowering_and_tree_lifting',
        'validation_graph_spec': proof.to_dict(), 'required_matching_checks': [],
        'required_path_checks': checks, 'path_certificates': certificates,
        'path_proof': {'bijection': 'one endpoint edge per unique tree path, including both endpoints in ALL',
                       'interior': 'fresh labels and common TRUE local valuation',
                       'growth': 'whole-query endpoint exclusions plus shared explicit FALSE prefix',
                       'scope': 'one physical bounded path and its copies; uniform chosen length; disjoint endpoint labels',
                       'small_families': small_certificates}}
    return PopulationResult(full, rows, [*render_state_checks(full.state_checks()), *checks], constraints, evidence)
