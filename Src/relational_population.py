"""Joint population of finite node/relationship matching atoms.

Bindings, labels, maps, directions and local predicates determine the graph.
Workload quantities attach to lexical atoms, and scalar obligations attach to
expressions. Neither role names nor a test-pattern registry are consulted.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product
import json

import ir
from dependencies import (analyze_dependencies, entity_binding_observation, matching_quantity_observation,
                          entity_symbol_origin, _clause_addresses, _close_row_fact)
from state import GraphSpec, render_state_checks
from population import (PopulationError, PopulationResult, MatchDemand, ValueDemand, EntityBindingDemand,
                        InversePair, BindingRelation, _located, _walk, _steps,
                        _address_id, _symbol_lookup, _typed, _bag, _compare,
                        _logical, _Interpreter, _Vertex, _affine)


def _conjuncts(value):
    if isinstance(value, ir.Boolean) and value.operator == "AND":
        return [part for term in value.terms for part in _conjuncts(term)]
    return [] if value is None else [value]


def _plain(value):
    return not any(isinstance(part, (ir.Count, ir.Exists, ir.All)) for part in _walk(value))


def _compatible(left, right):
    return all(key not in right or _typed(value) == _typed(right[key]) for key, value in left.items())


def _other(value):
    if value is None:
        return True
    if type(value) is bool:
        return not value
    if type(value) is int:
        return value + 1 if value < 2**63 - 1 else value - 1
    if type(value) is float:
        return value + 1.0
    return value + "_other"


def _types(edge):
    return ((edge.relationship.type,) if edge.relationship.type else ()) + edge.relationship.alternatives


@dataclass
class _Profile:
    name: str
    labels: list
    fixed: dict
    keys: set
    domains: dict
    origins: set


def _forest(source, alternatives):
    return [(('query',), source), *((('alternatives', i), tree) for i, tree in enumerate(alternatives))]


class _Problem:
    def __init__(self, source, alternatives, demands, obligations, scale, seed, correspondence=None):
        self.trees = _forest(source, alternatives)
        self.source, self.alternatives = source, alternatives
        self.scale, self.seed = scale, seed
        self.explicit_correspondence = correspondence is not None
        if correspondence is not None and any(not isinstance(link, BindingRelation) for link in correspondence):
            raise PopulationError('A binding correspondence must contain actual Node-address relations')
        self.demands = tuple(demands)
        self.obligations = (*tuple(obligations), *tuple(correspondence or ()))
        self.profiles, self.node_profiles, self.occurrences = [], {}, []
        self.lookup, self.infos, self.symbol_profiles = {}, [], {}
        self.components, self.node_work = [], []
        self._collect()

    def _collect(self):
        # First gather each lexical binding. Source/target bindings are aligned
        # only by compatible actual domains and names; separate a/b bindings
        # remain separate even when their labels happen to coincide.
        for prefix, tree in self.trees:
            try:
                ir.validate_query(tree)
                analysis = analyze_dependencies(tree)
            except ValueError as error:
                raise PopulationError(str(error)) from error
            located = list(_located(tree))
            symbols = _symbol_lookup(analysis)
            # A bare RETURN variable has both an input use and an output
            # definition at the same address. Scalar reads follow the input
            # edge, rather than recursively treating the output as its source.
            symbols.update({use['node_id']: use['symbol_id'] for use in analysis.uses})
            info = {'prefix': prefix, 'tree': tree, 'analysis': analysis,
                    'located': located, 'symbols': symbols, 'lookup': dict(located)}
            records = {record['symbol_id']: record for record in analysis.symbols}
            def node_origin(sid, seen=frozenset()):
                record = records.get(sid, {})
                producer = record.get('producer_node')
                produced = next(((path, part) for path, part in located if _address_id(path) == producer), None)
                if record.get('kind') == 'node' and produced and isinstance(produced[1], ir.Variable):
                    if sid in seen:
                        raise PopulationError('A node alias has a cyclic entity producer')
                    return node_origin(symbols.get(producer), seen | {sid})
                return sid
            info['node_origins'] = {sid: node_origin(sid) for sid, record in records.items() if record['kind'] == 'node'}
            self.infos.append(info)
            local = {}
            for address, value in located:
                self.lookup[(*prefix, *address)] = value
                if not isinstance(value, ir.Node):
                    continue
                symbol = symbols.get(_address_id((*address, 'variable')))
                symbol = info['node_origins'].get(symbol, symbol)
                if symbol is None:
                    raise PopulationError('A matching node has no lexical binding')
                record = local.setdefault(symbol, {'nodes': [], 'addresses': []})
                record['nodes'].append(value)
                record['addresses'].append(address)
            for symbol, record in local.items():
                name = record['nodes'][0].variable
                labels, fixed = [], {}
                for node in record['nodes']:
                    labels.extend(label for label in node.labels if label not in labels)
                    maps = {key: val.value for key, val in node.properties}
                    if not _compatible(fixed, maps):
                        raise PopulationError('One lexical node has incompatible fixed properties')
                    fixed.update(maps)
                if 'uid' in fixed:
                    raise PopulationError('Explicit UID maps require a separate identity allocation constraint')
                def separated(pid):
                    addresses = {(*prefix, *address) for address in record['addresses']}
                    for obligation in self.obligations:
                        if not isinstance(obligation, BindingRelation) or obligation.equal:
                            continue
                        left, right = tuple(obligation.left_address), tuple(obligation.right_address)
                        if (left in addresses and self.node_profiles.get(right) == pid
                                or right in addresses and self.node_profiles.get(left) == pid):
                            return True
                    return False
                # A lexical symbol is the binding identity. Across trees a
                # unique actual domain can align copies of a law; within one
                # tree only a compatible *nonempty fixed map* permits physical
                # co-location of different symbols (e.g. UNION's keyed anchor).
                # Printed variable names never establish such an identity.
                candidates = [i for i, profile in enumerate(self.profiles)
                              if not separated(i) and _compatible(profile.fixed, fixed)
                              and (not self.explicit_correspondence or prefix in profile.origins)
                              and (bool(fixed) and bool(profile.fixed)
                                   and _typed(fixed) == _typed(profile.fixed)
                                   and bool(set(labels) & set(profile.labels))
                                   or prefix not in profile.origins
                                   and set(labels) == set(profile.labels)
                                   and bool(labels))]
                if len(candidates) > 1:
                    raise PopulationError('Ambiguous cross-query binding alignment requires BindingRelation')
                if candidates:
                    pid = candidates[0]
                    profile = self.profiles[pid]
                    profile.labels.extend(label for label in labels if label not in profile.labels)
                    profile.fixed.update(fixed)
                    profile.origins.add(prefix)
                else:
                    pid = len(self.profiles)
                    self.profiles.append(_Profile(name, labels, fixed, set(), {}, {prefix}))
                self.symbol_profiles[(prefix, symbol)] = pid
                for address in record['addresses']:
                    self.node_profiles[(*prefix, *address)] = pid
            for sid, origin in info['node_origins'].items():
                if (prefix, origin) in self.symbol_profiles:
                    self.symbol_profiles[(prefix, sid)] = self.symbol_profiles[(prefix, origin)]
            # Property domains belong to resolved symbols, including alias
            # producer expressions; matching a printed variable name is unsafe.
            for address, value in located:
                if isinstance(value, ir.Property):
                    sid = symbols.get(_address_id(address))
                    pid = self.symbol_profiles.get((prefix, sid))
                    if pid is not None and value.key != 'uid':
                        self.profiles[pid].keys.add(value.key)
                if isinstance(value, ir.Comparison):
                    for field, prop, literal in (('left', value.left, value.right), ('right', value.right, value.left)):
                        if isinstance(prop, ir.Property) and isinstance(literal, ir.Literal):
                            sid = symbols.get(_address_id((*address, field)))
                            pid = self.symbol_profiles.get((prefix, sid))
                            if pid is not None and prop.key != 'uid':
                                domain = self.profiles[pid].domains.setdefault(prop.key, [])
                                for candidate in (literal.value, _other(literal.value), None):
                                    if _typed(candidate) not in {_typed(item) for item in domain}:
                                        domain.append(candidate)
        if not self.profiles:
            raise PopulationError('Relational population requires at least one matching node')
        self._binding_obligations()
        self._scalar_domains()
        all_labels = {label for profile in self.profiles for label in profile.labels}
        for i, profile in enumerate(self.profiles):
            if not profile.labels:
                label = f'GspEntity{i}'
                while label in all_labels:
                    label += '_'
                profile.labels.append(label)
                all_labels.add(label)
        for info in self.infos:
            prefix = info['prefix']
            for address, value in info['located']:
                if not isinstance(value, (ir.Node, ir.Edge)):
                    continue
                # An Edge's two Node children are constraints on that atom,
                # not independent scans or independent population demands.
                if isinstance(value, ir.Node) and isinstance(info['lookup'].get(address[:-1]), ir.Edge):
                    continue
                parent = info['lookup'].get(address[:-1])
                predicate = getattr(parent, 'where', None)
                if isinstance(value, ir.Edge):
                    if value.relationship.min_hops is not None or value.relationship.max_hops is not None or value.path_variable:
                        raise PopulationError('The relational engine accepts fixed-length relationship atoms; use the bounded-path solver')
                    if not _types(value):
                        raise PopulationError('A relationship atom needs an explicit finite type domain')
                    left = self.node_profiles[(*prefix, *address, 'left')]
                    right = self.node_profiles[(*prefix, *address, 'right')]
                    if left == right:
                        raise PopulationError('Self-loop/repeated-endpoint atoms need a separate equality multiplicity rule')
                    occurrence = {'address': (*prefix, *address), 'pattern': value,
                                  'left': left, 'right': right, 'predicate': predicate, 'info': info}
                else:
                    occurrence = {'address': (*prefix, *address), 'pattern': value,
                                  'profile': self.node_profiles[(*prefix, *address)],
                                  'predicate': predicate, 'info': info}
                self.occurrences.append(occurrence)
        self._workloads()

    def _value_properties(self, info, address, seen=frozenset()):
        """Resolve value-producing property leaves through actual def-use edges.

        CASE guards select a value but do not produce it. COUNT/EXISTS produce
        values from row semantics, so their matching obligations remain in the
        quantity solver rather than seeding their local node fields.
        """
        value = info['lookup'].get(address)
        if isinstance(value, ir.Property):
            sid = info['symbols'].get(_address_id(address))
            pid = self.symbol_profiles.get((info['prefix'], sid))
            if pid is not None and value.key != 'uid':
                yield pid, value.key
        elif isinstance(value, ir.Variable):
            sid = info['symbols'].get(_address_id(address))
            if sid in seen:
                raise PopulationError('A scalar value domain has a cyclic producer')
            record = next((record for record in info['analysis'].symbols if record['symbol_id'] == sid), {})
            producer = record.get('producer_node')
            location = next((path for path, _ in info['located'] if _address_id(path) == producer), None)
            if location is not None:
                yield from self._value_properties(info, location, seen | {sid})
        elif isinstance(value, ir.Case):
            for field in ('then', 'otherwise'):
                yield from self._value_properties(info, (*address, field), seen)
        elif isinstance(value, ir.Call) and value.name == 'coalesce':
            for i in range(len(value.args)):
                yield from self._value_properties(info, (*address, 'args', i), seen)

    def _scalar_domains(self):
        def seed(info, address, value):
            for pid, key in self._value_properties(info, address):
                profile = self.profiles[pid]
                profile.keys.add(key)
                domain = profile.domains.setdefault(key, [])
                for candidate in (value, _other(value), None):
                    if _typed(candidate) not in {_typed(item) for item in domain}:
                        domain.append(candidate)

        # Comparisons can use WITH aliases instead of direct properties. Their
        # literal domains belong to the producer symbol, not the printed name.
        for info in self.infos:
            for address, value in info['located']:
                if isinstance(value, ir.Comparison):
                    for field, literal in (('left', value.right), ('right', value.left)):
                        if isinstance(literal, ir.Literal):
                            seed(info, (*address, field), literal.value)
        for obligation in self.obligations:
            if not isinstance(obligation, ValueDemand):
                continue
            if obligation.relation not in ('equal', 'nonnull'):
                raise PopulationError('ValueDemand relation must be equal or nonnull')
            if obligation.relation == 'equal':
                try:
                    ir._validate_literal(ir.Literal(obligation.value))
                except ir.QueryError as error:
                    raise PopulationError(str(error)) from error
            address = tuple(obligation.address)
            info = next((info for info in self.infos if address[:len(info['prefix'])] == info['prefix']), None)
            if info is None or address not in self.lookup:
                raise PopulationError('ValueDemand must address an expression in the source/target forest')
            if obligation.relation == 'equal':
                seed(info, address[len(info['prefix']):], obligation.value)

    def _binding_obligations(self):
        for obligation in self.obligations:
            if not isinstance(obligation, BindingRelation):
                continue
            try:
                left = self.node_profiles[tuple(obligation.left_address)]
                right = self.node_profiles[tuple(obligation.right_address)]
            except KeyError as error:
                raise PopulationError('BindingRelation must address matching Node occurrences') from error
            if not obligation.equal:
                if left == right:
                    raise PopulationError('A requested pair of physical bindings was already identified')
                continue
            if left == right:
                continue
            a, b = self.profiles[left], self.profiles[right]
            if not _compatible(a.fixed, b.fixed):
                raise PopulationError('Equal bindings have incompatible fixed values')
            a.labels.extend(label for label in b.labels if label not in a.labels)
            a.fixed.update(b.fixed)
            a.origins.update(b.origins)
            a.keys.update(b.keys)
            for key, values in b.domains.items():
                domain = a.domains.setdefault(key, [])
                for value in values:
                    if _typed(value) not in {_typed(item) for item in domain}:
                        domain.append(value)
            self.node_profiles = {address: left if pid == right else pid for address, pid in self.node_profiles.items()}
            self.symbol_profiles = {symbol: left if pid == right else pid for symbol, pid in self.symbol_profiles.items()}
            # Keep the profile slot but mark it unused. IDs in dependency maps
            # must not silently shift after one equality merge.
            b.labels, b.fixed, b.keys, b.domains = [], {}, set(), {}
        for obligation in self.obligations:
            if isinstance(obligation, BindingRelation) and not obligation.equal:
                if self.node_profiles[tuple(obligation.left_address)] == self.node_profiles[tuple(obligation.right_address)]:
                    raise PopulationError('Explicit binding equality contradicts a required separation')

    def _workloads(self):
        occurrence_by_address = {item['address']: item for item in self.occurrences}
        demands = {}
        for demand in self.demands:
            if not isinstance(demand, MatchDemand) or tuple(demand.address) not in occurrence_by_address:
                raise PopulationError('A MatchDemand must address an actual Node or Edge matching atom')
            for value in (demand.min_matches, demand.exact_matches, demand.candidate_matches):
                if value is not None and (type(value) is not int or value < 0):
                    raise PopulationError('Matching quantities must be nonnegative integers')
            if not demand.role_atom or type(demand.scale_matches) is not bool:
                raise PopulationError('A matching demand needs a provenance role and boolean growth flag')
            if demand.candidate_kind not in (None, 'right', 'right_scalar', 'left_scalar'):
                raise PopulationError('candidate_kind must select an actual endpoint or endpoint scalar exclusion')
            demands.setdefault(tuple(demand.address), []).append(demand)
        self.roles = {}
        for occurrence in self.occurrences:
            pattern = occurrence['pattern']
            attached = demands.get(occurrence['address'], [])
            occurrence['demands'] = attached
            if isinstance(pattern, ir.Node):
                key = occurrence['profile']
                work = next((item for item in self.node_work if item['profile'] == key), None)
                if work is None:
                    work = {'profile': key, 'occurrences': [], 'demands': []}
                    self.node_work.append(work)
                work['occurrences'].append(occurrence)
                work['demands'].extend(attached)
                occurrence['works'] = [work]
            else:
                occurrence['works'] = []
                types = _types(pattern)
                for index, relation_type in enumerate(types):
                    left, right = occurrence['left'], occurrence['right']
                    source, target = (right, left) if pattern.direction == 'in' else (left, right)
                    unordered = pattern.direction == 'undirected'
                    key = (tuple(sorted((source, target))) if unordered else (source, target),
                           relation_type, _typed(dict((key, val.value) for key, val in pattern.relationship.properties)))
                    work = next((item for item in self.components if item['key'] == key), None)
                    if work is None:
                        work = {'key': key, 'source': source, 'target': target, 'type': relation_type,
                                'left': left, 'right': right, 'occurrences': [], 'demands': [],
                                'properties': {key: val.value for key, val in pattern.relationship.properties},
                                'default': 1 if index == 0 else 0}
                        self.components.append(work)
                    work['occurrences'].append(occurrence)
                    work['demands'].extend(demand for demand in attached
                                           if demand.relationship_type == relation_type or
                                           demand.relationship_type is None and index == 0)
                    occurrence['works'].append(work)
                if any(demand.relationship_type is not None and demand.relationship_type not in types for demand in attached):
                    raise PopulationError('A workload selected a type outside the atom alternatives')
        for work in (*self.node_work, *self.components):
            exact = {demand.exact_matches for demand in work['demands'] if demand.exact_matches is not None}
            if len(exact) > 1:
                raise PopulationError('The same matching domain has incompatible exact quantities')
            minimum = max([demand.min_matches for demand in work['demands']] or [0])
            growing = any(demand.scale_matches for demand in work['demands'])
            quantity = next(iter(exact)) if exact else max(minimum, work.get('default', 1))
            if exact and quantity < minimum or growing and exact:
                raise PopulationError('Incompatible exact/minimum/scale matching requirements')
            work.update(quantity=quantity, growing=growing,
                        candidates=max([demand.candidate_matches or 0 for demand in work['demands']] or [0]))
            for demand in work['demands']:
                self.roles.setdefault(demand.role_atom, []).append(work)
        # An inverse is one relation represented twice on the same vertex pair.
        # It is never populated as two unrelated endpoint populations.
        for obligation in self.obligations:
            if not isinstance(obligation, InversePair):
                continue
            forward = [work for work in self.components if work['type'] == obligation.forward_type]
            reverse = [work for work in self.components if work['type'] == obligation.reverse_type]
            if not forward:
                raise PopulationError('An inverse obligation has no forward relationship atom')
            for work in reverse:
                matches = [item for item in forward if item['source'] == work['target'] and item['target'] == work['source']]
                if len(matches) != 1:
                    raise PopulationError('Inverse atoms must use the same resolved vertex domains in reverse order')
                origin = matches[0]
                if work['demands'] and (work['quantity'] != origin['quantity'] or work['growing'] != origin['growing']):
                    raise PopulationError('Inverse pair quantities conflict')
                origin['candidates'] = max(origin['candidates'], work['candidates'])
                work['mirror'] = origin
                work['quantity'], work['growing'] = origin['quantity'], origin['growing']
            for work in forward:
                work['inverse'] = obligation

    def assignments(self):
        used = tuple(sorted(set(self.node_profiles.values())))
        free = {pid: tuple(sorted(self.profiles[pid].keys - self.profiles[pid].fixed.keys())) for pid in used}
        if sum(map(len, free.values())) > 12:
            raise PopulationError('Finite relational property solving is limited to twelve free properties')
        constraints = self._positive_local_constraints()

        def local_models(pid):
            profile = self.profiles[pid]
            predicates = [(term, {part.key for part in _walk(term) if isinstance(part, ir.Property)})
                          for term in constraints.get(pid, ())]
            # A predicate is checked as soon as its fields are assigned. This
            # preserves correlations between fields while pruning FALSE/NULL
            # positive-witness models before other profiles are combined.
            keys = sorted(free[pid], key=lambda key: (-sum(key in fields for _, fields in predicates), key))
            values = dict(profile.fixed)
            from population import _scalar

            def extend(index):
                for term, fields in predicates:
                    if not fields <= values.keys():
                        continue
                    try:
                        if _scalar(term, values) is not True:
                            return
                    except PopulationError:
                        return
                if index == len(keys):
                    yield dict(values)
                    return
                key = keys[index]
                for value in profile.domains.get(key, [True, False, None]):
                    values[key] = value
                    yield from extend(index + 1)
                del values[key]

            yield from extend(0)

        assignment = [dict(profile.fixed) for profile in self.profiles]

        def combine(index):
            if index == len(used):
                yield [dict(values) for values in assignment]
                return
            pid = used[index]
            for values in local_models(pid):
                assignment[pid] = values
                yield from combine(index + 1)

        yield from combine(0)

    def _local_terms(self, occurrence):
        # A scalar clause guard can deliberately eliminate a whole expansion.
        # It is not a demand that the underlying matching atom be empty.
        pattern = occurrence['pattern']
        names = ({pattern.variable} if isinstance(pattern, ir.Node) else {pattern.left.variable, pattern.right.variable})
        return [term for term in _conjuncts(occurrence['predicate']) if _plain(term)
                and any(isinstance(part, ir.Property) and part.variable in names for part in _walk(term))]

    def _positive_local_constraints(self):
        # Ensure ordinary witnesses satisfy genuine local scalar predicates.
        # ValueDemand can require a root guard false; such a demand overrides
        # witness filtering of that root expression, but not an endpoint test.
        constraints = {}
        for work in self.components:
            if work.get('mirror') or work['quantity'] == 0 and not work['growing']:
                continue
            for occurrence in work['occurrences']:
                pattern = occurrence['pattern']
                right = occurrence['right']
                for term in self._local_terms(occurrence):
                    refs = {part.variable for part in _walk(term) if isinstance(part, ir.Property)}
                    if refs != {pattern.right.variable}:
                        continue
                    constraints.setdefault(right, []).append(term)
        return constraints

    def _true_properties(self, assignment):
        from population import _scalar
        try:
            return all(_scalar(term, assignment[pid]) is True
                       for pid, terms in self._positive_local_constraints().items() for term in terms)
        except PopulationError:
            return False

    def _near_miss(self, work, assignment):
        requirements = [(next(occurrence for occurrence in work['occurrences']
                              if occurrence['address'] == tuple(demand.address)), demand.candidate_kind)
                        for demand in work['demands'] if demand.candidate_kind is not None]
        for near in self._candidate_near_misses(work, assignment):
            if all(self._candidate_conforms(occurrence, kind, near) for occurrence, kind in requirements):
                return near
        kind = ' with the required right population/scalar exclusion' if requirements else ''
        raise PopulationError('Candidate growth has no actual local near-miss condition' + kind)

    def _candidate_conforms(self, occurrence, kind, near):
        pattern = occurrence['pattern']
        side = 'left' if kind == 'left_scalar' else 'right'
        node = pattern if isinstance(pattern, ir.Node) else getattr(pattern, side)
        pid = occurrence['profile'] if isinstance(pattern, ir.Node) else occurrence[side]
        if near['profile'] != pid:
            return False
        profile = self.profiles[pid]
        structural = (set(node.labels).issubset(near['labels'])
                      and all(_compare(near['values'].get(key), '=', literal.value) is True
                              for key, literal in node.properties))
        if kind in ('right_scalar', 'left_scalar'):
            if (set(near['labels']) != set(profile.labels)
                    or not all(_typed(near['values'].get(key)) == _typed(value) for key, value in profile.fixed.items())):
                return False
        from population import _scalar
        for term in self._local_terms(occurrence):
            if {part.variable for part in _walk(term) if isinstance(part, ir.Property)} != {node.variable}:
                continue
            try:
                if _scalar(term, near['values']) is not True:
                    return structural if kind in ('right_scalar', 'left_scalar') else True
            except PopulationError:
                continue
        return kind == 'right' and not structural

    def _candidate_near_misses(self, work, assignment):
        # Search real failed tests, rather than attaching a distinguished label
        # or setting a hard-coded property. One condition changes at a time.
        for occurrence in work['occurrences']:
            pattern = occurrence['pattern']
            candidates = ([('profile', pattern)] if isinstance(pattern, ir.Node) else
                          [('right', pattern.right), ('left', pattern.left)])
            for side, node in candidates:
                pid = occurrence[side]
                profile = self.profiles[pid]
                for term in self._local_terms(occurrence):
                    refs = {part.variable for part in _walk(term) if isinstance(part, ir.Property)}
                    if refs != {node.variable}:
                        continue
                    # An exact-zero matching domain may already have the
                    # required FALSE or NULL filter value. Preserve that
                    # rejected valuation instead of changing NULL to FALSE.
                    # Positive witnesses are separately required to pass the
                    # filter, so this branch cannot weaken matching semantics.
                    from population import _scalar
                    try:
                        already_rejected = _scalar(term, assignment[pid]) is not True
                    except PopulationError:
                        already_rejected = False
                    if already_rejected:
                        yield {'profile': pid, 'labels': list(profile.labels), 'values': assignment[pid],
                                'reason': {'kind': 'scalar_condition', 'mode': 'already_rejected',
                                           'atom': list(occurrence['address'])}}
                    for key in sorted({part.key for part in _walk(term) if isinstance(part, ir.Property)}):
                        if key == 'uid':
                            continue
                        for val in profile.domains.get(key, [_other(assignment[pid].get(key)), None]):
                            changed = assignment[pid] | {key: val}
                            try:
                                false = _scalar(term, changed) is not True
                            except PopulationError:
                                false = False
                            if false:
                                yield {'profile': pid, 'labels': list(profile.labels), 'values': changed,
                                        'reason': {'kind': 'scalar_condition', 'key': key, 'value': val,
                                                   'atom': list(occurrence['address'])}}
                if len(node.labels) > 1:
                    labels = [label for label in profile.labels if label != node.labels[0]]
                    yield {'profile': pid, 'labels': labels, 'values': assignment[pid],
                            'reason': {'kind': 'missing_label', 'label': node.labels[0], 'atom': list(occurrence['address'])}}
                if node.properties:
                    key, literal = node.properties[0]
                    yield {'profile': pid, 'labels': list(profile.labels),
                            'values': assignment[pid] | {key: _other(literal.value)},
                            'reason': {'kind': 'fixed_property', 'key': key, 'atom': list(occurrence['address'])}}
            # For relationship expansion even a one-label endpoint check is a
            # genuine rejection after traversing the matching relationship.
            if isinstance(pattern, ir.Edge) and pattern.right.labels:
                pid = occurrence['right']
                labels = [label for label in self.profiles[pid].labels if label != pattern.right.labels[0]]
                if not labels:
                    label = 'GspEntity'
                    all_labels = {value for profile in self.profiles for value in profile.labels}
                    while label in all_labels:
                        label += '_'
                    labels = [label]
                yield {'profile': pid, 'labels': labels, 'values': assignment[pid],
                        'reason': {'kind': 'missing_label', 'label': pattern.right.labels[0], 'atom': list(occurrence['address'])}}

    def realize(self, assignment, *, proof=False):
        active = min(self.scale, 3) if proof else self.scale
        used = sorted(set(self.node_profiles.values()))
        quantities = {id(work): active if work['growing'] else work['quantity'] for work in self.components}
        counts = {pid: 1 for pid in used}
        growing_profiles = set()
        for work in self.node_work:
            counts[work['profile']] = active if work['growing'] else work['quantity']
            if work['growing']:
                growing_profiles.add(work['profile'])
        # Directed binding dependencies allocate a fresh target block for every
        # source witness. A chain consequently joins on its real middle entity.
        pending = [work for work in self.components if not work.get('mirror')]
        for _ in range(len(pending) + 1):
            changed = False
            for work in pending:
                left, right = work['left'], work['right']
                # A fixed map identifies an existing anchor even when an
                # equivalent query starts the relationship at its other end.
                if self.profiles[right].fixed:
                    continue
                needed = counts[left] * quantities[id(work)]
                if needed > 4096 and not work['growing'] and left not in growing_profiles:
                    raise PopulationError('Witness product exceeds the finite graph-construction bound')
                if counts[right] < needed:
                    counts[right] = needed
                    changed = True
                    if work['growing'] or left in growing_profiles:
                        growing_profiles.add(right)
            if not changed:
                break
        else:
            raise PopulationError('A cyclic binding graph needs a separate finite fixed-point rule')
        families, noise_by_work = [], {}
        # The stable witness prefix is allocated before all growing families,
        # and every witness before every failed candidate. UID observations of
        # unaffected witnesses therefore agree between proof/full-scale graphs.
        for pid in sorted(used, key=lambda value: (value in growing_profiles, value)):
            if counts[pid]:
                families.append({'profile': pid, 'count': counts[pid], 'labels': list(self.profiles[pid].labels),
                                 'values': assignment[pid], 'kind': 'valid', 'growing': pid in growing_profiles})
        for work in (*self.node_work, *pending):
            q = (active if work['growing'] else work['quantity'])
            extra = max(0, work['candidates'] - q)
            if not extra:
                continue
            near = self._near_miss(work, assignment)
            # A relationship candidate quantity is relative to one source
            # binding. Node-scan candidates are global to the node domain.
            if 'left' in work and near['profile'] == work['right']:
                extra *= counts[work['left']]
            if proof:
                extra = min(extra, max(2, counts.get(work.get('left'), 1) * 2))
            near.update(count=extra, kind='rejected', growing=True)
            families.append(near)
            noise_by_work[id(work)] = near
        uid = 1
        for family in families:
            family['base'] = uid
            uid += family['count']
            if uid >= 2**63:
                raise PopulationError('Graph UID allocation exceeds the signed 64-bit domain')
        main = {family['profile']: family for family in families if family['kind'] == 'valid'}
        node_groups = []
        for family in families:
            properties = dict(family['values'])
            properties['uid'] = family['base'] if family['count'] == 1 else _affine(family['base'])
            node_groups.append({'count': family['count'], 'labels': family['labels'], 'properties': properties})
        edge_groups, work_groups = [], {}

        def connect(work, source, target, quantity, *, noise=False):
            if not source or not target or not quantity:
                return []
            m, t = source['count'], target['count']
            if quantity > t:
                raise PopulationError('The requested matching quantity needs parallel-edge multiplicity outside this construction normal form')
            if m <= 64:
                # Emit one affine target interval per source. For a chain,
                # each source gets its own block when the domain permits it.
                blocks = [(1, source['base'] + i,
                           target['base'] + (i * quantity if t >= m * quantity else 0), quantity)
                          for i in range(m)]
            elif quantity <= 64:
                blocks = [(m, _affine(source['base']), target['base'] + j, 1) for j in range(quantity)]
            else:
                raise PopulationError('Two simultaneously unbounded fanout dimensions need a product-family representation')
            result = []
            for many, start, end, width in blocks:
                count = width if many == 1 else many
                src = start
                dst = end if count == 1 or many != 1 else _affine(end)
                # Internal source/target orientation follows physical edges;
                # the matching atom may traverse the same edge in either way.
                logical_left = work['left']
                if source['profile'] != logical_left:
                    raise PopulationError('Inconsistent logical source domain')
                source_label, target_label = source['labels'][0], target['labels'][0]
                if work['source'] != logical_left:
                    src, dst = dst, src
                    source_label, target_label = target_label, source_label
                group = {'count': count, 'source': src, 'target': dst,
                         'source_label': source_label, 'target_label': target_label,
                         'type': work['type'], 'properties': dict(work['properties'])}
                result.append(group)
            return result

        for work in pending:
            groups = []
            left, right = work['left'], work['right']
            q = quantities[id(work)]
            # Valid vertices and any rejected vertices in the left domain have
            # the same neighborhood production. Thus moving a node restriction
            # after this expansion actually encounters the near-miss vertices.
            for source_family in [family for family in families if family['profile'] == left]:
                groups.extend(connect(work, source_family, main.get(right), q))
            noise = noise_by_work.get(id(work))
            if noise and noise['profile'] == right:
                qnoise = max(0, work['candidates'] - q)
                if proof:
                    qnoise = min(qnoise, 2)
                groups.extend(connect(work, main.get(left), noise, qnoise, noise=True))
            pair = work.get('inverse')
            if pair and pair.pair_key:
                start = sum(item['count'] for item in edge_groups) + 1
                for group in groups:
                    group['properties'][pair.pair_key] = start if group['count'] == 1 else _affine(start)
                    start += group['count']
            edge_groups.extend(groups)
            work_groups[id(work)] = groups
            if pair:
                edge_groups.extend({**group, 'source': group['target'], 'target': group['source'],
                                    'source_label': group['target_label'], 'target_label': group['source_label'],
                                    'type': pair.reverse_type, 'properties': dict(group['properties'])}
                                   for group in groups)
        if not node_groups:
            # An exact-zero node domain still needs a harmless physical graph
            # for GraphSpec's explicit v2 contract, outside every input label.
            label = 'GspEntity'
            while label in {label for profile in self.profiles for label in profile.labels}:
                label += '_'
            node_groups = [{'count': 1, 'labels': [label], 'properties': {'uid': 1}}]
        # One actual fixed-map access path is enough for the current backend
        # schema contract. It is derived from query constraints, and differs
        # from the transient UID index used solely while loading large groups.
        accesses = [(profile.labels[0], key) for pid, profile in enumerate(self.profiles)
                    if pid in used and profile.labels for key in profile.fixed if key != 'uid']
        indexes = (() if not accesses else ({'name': 'gsp_cost_lookup', 'label': accesses[0][0],
                                             'property': accesses[0][1]},))
        spec = GraphSpec('AST', self.scale, self.seed, version=2, include_reference=False,
                         node_groups=tuple(node_groups), edge_groups=tuple(edge_groups), index_requirements=indexes)
        return spec, families, main, quantities

    def value_demands_hold(self, assignment, spec, main):
        for obligation in self.obligations:
            if not isinstance(obligation, ValueDemand):
                continue
            if obligation.relation not in ('equal', 'nonnull'):
                raise PopulationError('ValueDemand relation must be equal or nonnull')
            address = tuple(obligation.address)
            value = self.lookup.get(address)
            info = next((info for info in self.infos if address[:len(info['prefix'])] == info['prefix']), None)
            if value is None or info is None:
                raise PopulationError('ValueDemand must address an expression in the source/target forest')
            local_address = address[len(info['prefix']):]
            try:
                values = _lexical_values(self, info, local_address, value, spec)
            except (PopulationError, KeyError):
                return False
            if obligation.relation == 'nonnull':
                if any(value is None for value in values):
                    return False
            elif any(_typed(value) != _typed(obligation.value) for value in values):
                return False
        return True

    def row_observations(self, *, proof=False):
        result = []
        for demand in (*self.demands, *self.obligations):
            if not isinstance(demand, (MatchDemand, EntityBindingDemand)):
                continue
            address = tuple(demand.address)
            info = next((info for info in self.infos if address[:len(info['prefix'])] == info['prefix']), None)
            if info is None:
                raise PopulationError('A binding or matching demand has no forest occurrence')
            local = address[len(info['prefix']):]
            try:
                if isinstance(demand, EntityBindingDemand):
                    fact = entity_binding_observation(info['tree'], local, demand.relation)
                    kind = 'binding'
                else:
                    exact = min(self.scale, 3) if proof and demand.scale_matches else self.scale if demand.scale_matches else demand.exact_matches
                    occurrence = next(item for item in self.occurrences if item['address'] == address)
                    work = next((work for work in occurrence['works'] if demand.relationship_type is None
                                 or work.get('type') == demand.relationship_type), occurrence['works'][0])
                    role = 'right' if isinstance(occurrence['pattern'], ir.Edge) and work['left'] == occurrence['right'] else 'left'
                    fact = matching_quantity_observation(info['tree'], local, minimum=demand.min_matches,
                                                         exact=exact, relationship_type=demand.relationship_type,
                                                         logical_source_role=role)
                    kind = 'matching quantity'
            except ir.QueryError as error:
                raise PopulationError(f'Invalid {"binding" if isinstance(demand, EntityBindingDemand) else "matching quantity"} observation: {error}') from error
            result.append((kind, info, fact))
        return result

    def row_demands_hold(self, spec, *, proof=False):
        for kind, _, fact in self.row_observations(proof=proof):
            query = ir.Query(None, None, (ir.Alias(fact['expression'], 'fact'),))
            if _Interpreter(spec).run(query) != [{'fact': True}]:
                raise PopulationError(f'An actual row-domain {kind} obligation is not satisfied')


def synthesize(source, *, alternatives=(), scale, data_seed, demands=(), obligations=(), result_rows=None,
               correspondence=None, min_result_rows=0):
    if type(scale) is not int or scale < 1 or type(data_seed) is not int or data_seed < 0:
        raise PopulationError('scale must be positive and data_seed nonnegative')
    if result_rows is not None and (type(result_rows) is not int or result_rows < 0):
        raise PopulationError('result_rows must be a nonnegative integer')
    if type(min_result_rows) is not int or min_result_rows < 0:
        raise PopulationError('min_result_rows must be a nonnegative integer')
    if correspondence is not None:
        correspondence = tuple(correspondence)
    if any(not isinstance(value, (ValueDemand, EntityBindingDemand, InversePair, BindingRelation)) for value in obligations):
        raise PopulationError('Unknown relational population obligation')
    problem = _Problem(source, tuple(alternatives), demands, obligations, scale, data_seed, correspondence)
    last_error = None
    for model, assignment in enumerate(problem.assignments()):
        if model >= 50_000:
            raise PopulationError('Relational finite-domain model budget exhausted; satisfiability is unknown')
        if not problem._true_properties(assignment):
            continue
        try:
            small, families, main, quantities = problem.realize(assignment, proof=True)
            small_families = families
            if not problem.value_demands_hold(assignment, small, main):
                continue
            problem.row_demands_hold(small, proof=True)
            interpreter = _Interpreter(small)
            rows = interpreter.run(source)
            if len(rows) < min_result_rows or result_rows is not None and len(rows) != result_rows:
                continue
            if any(_bag(_Interpreter(small).run(tree)) != _bag(rows) for tree in alternatives):
                continue
            full, full_families, _, full_quantities = problem.realize(assignment)
            # This independent full evaluation is useful at ordinary test
            # scales. The certificate below separately checks every growing
            # family before a larger graph can be accepted.
            if full.counts()['nodes'] <= 5000:
                if not problem.value_demands_hold(assignment, full, {}):
                    continue
                problem.row_demands_hold(full)
                actual = _Interpreter(full).run(source)
                if _bag(actual) != _bag(rows):
                    continue
                if any(_bag(_Interpreter(full).run(tree)) != _bag(rows) for tree in alternatives):
                    continue
            certificates = _certify_growth(problem, assignment, small, full_families, full)
            row_certificates = _certify_row_demands(problem, assignment, small, full, small_families, full_families)
            scalar_certificates = _certify_value_demands(problem, assignment, small)
            assignment_evidence = {}
            for role, works in problem.roles.items():
                unique = list({id(work): work for work in works}.values())
                valid = sum(full_quantities[id(work)] if 'type' in work else
                            (scale if work['growing'] else work['quantity']) for work in unique)
                assignment_evidence[role] = {'valid_matches': valid,
                    'candidate_matches': sum(max(work['candidates'], scale if work['growing'] else work['quantity']) for work in unique),
                    'relationship_types': [work['type'] for work in unique if 'type' in work],
                    'endpoint_labels': sorted({label for work in unique
                                               for label in problem.profiles[work.get('right', work.get('profile'))].labels})}
                if len(unique) == 1 and 'type' in unique[0]:
                    assignment_evidence[role]['relationship_type'] = unique[0]['type']
            components = [{'addresses': [list(item['address']) for item in work['occurrences']],
                           'left_binding': problem.profiles[work['left']].name,
                           'right_binding': problem.profiles[work['right']].name,
                           'relationship_type': work['type'], 'valid_matches': full_quantities[id(work)],
                           'candidate_matches': max(work['candidates'], full_quantities[id(work)])}
                          for work in problem.components]
            constraints = {'assignment': assignment_evidence, 'components': components,
                           'binding_domains': [{'profile': i, 'name': profile.name, 'labels': profile.labels,
                                               'properties': assignment[i]} for i, profile in enumerate(problem.profiles)],
                           'obligations': json.loads(json.dumps([{'kind': type(value).__name__, **value.__dict__}
                                                                 for value in obligations]))}
            if correspondence is not None:
                constraints['binding_correspondence'] = json.loads(json.dumps([link.__dict__ for link in correspondence]))
            evidence = {'construction': 'joint_binding_graph_finite_model',
                        'validation_graph_spec': small.to_dict(), 'subquery_outcomes': interpreter.subquery_outcomes,
                        'growth_certificates': certificates, 'physical_cost_guaranteed': False,
                        'scalar_value_certificates': scalar_certificates,
                        'row_observation_certificates': row_certificates,
                        'scope': 'finite fixed-length binding graph; scalar local predicates; explicit inverse and null-row obligations',
                        'witness_uid_allocation': 'all stable valid bindings precede growing families and rejected candidates'}
            evidence['binding_correspondence'] = 'explicit_origin_relations' if correspondence is not None else 'legacy_domain_alignment'
            from query import render_query
            matching_checks = []
            for kind, _, fact in problem.row_observations():
                if kind != 'matching quantity':
                    continue
                check = {'query': render_query(ir.Query(None, None, (ir.Alias(fact['expression'], 'fact'),))),
                         'expected_rows': [{'fact': True}]}
                if check not in matching_checks:
                    matching_checks.append(check)
            evidence['required_matching_checks'] = matching_checks
            return PopulationResult(full, rows, [*render_state_checks(full.state_checks()), *matching_checks], constraints, evidence)
        except PopulationError as error:
            last_error = error
    raise PopulationError('No relational model satisfies the whole-query bag and scalar/quantity obligations'
                          + (f': {last_error}' if last_error else ''))


def _certify_growth(problem, assignment, small, families, full):
    """Check observation closure for each variable-size physical domain.

    A failed candidate must be rejected at every unmasked graph read. A valid
    family needs an AST consumer whose value is determined without that read.
    The proof is structural, not extrapolation from a few cardinality probes.
    """
    certificates = []
    problem.growth_context = (families, full)
    for family in families:
        if not family['growing']:
            continue
        observations = []
        for occurrence in problem.occurrences:
            for bindings in _family_reads(problem, occurrence, family, families, full):
                excluded = _local_exclusion(problem, occurrence, bindings, assignment)
                mask = _masking_ancestor(problem, occurrence, assignment, small, bindings)
                if not excluded and mask is None:
                    raise PopulationError('A growing node/relationship family is visible to a graph read outside an exclusion or masking certificate')
                observations.append({'address': list(occurrence['address']), 'excluded': excluded, 'mask': mask})
        certificates.append({'family_kind': family['kind'], 'profile': family['profile'],
                             'reason': family.get('reason'), 'observations': observations})
    return certificates


def _node_can_read(occurrence, side, node, family):
    """Physical labels/maps, plus the actual imported binding definition."""
    constraints = [node]
    info = occurrence['info']
    address = occurrence['address'][len(info['prefix']):]
    node_address = address if side == 'profile' else (*address, side)
    sid = info['symbols'].get(_address_id((*node_address, 'variable')))
    definition = next((item['definition'] for item in info['analysis'].symbols if item['symbol_id'] == sid), None)
    if sid is not None:
        definition = entity_symbol_origin(info['analysis'], sid)['definition']
    if definition != _address_id((*node_address, 'variable')) and definition is not None:
        definitions = {_address_id(path): part for path, part in info['located']}
        imported = definitions.get(definition.rsplit('/', 1)[0])
        if isinstance(imported, ir.Node):
            constraints.append(imported)
            # A non-NULL imported node also passed the local predicates of its
            # defining match. OPTIONAL only adds NULL when these fail; it never
            # binds a rejected physical vertex and later reintroduces it.
            definition_path = next((path for path, value in info['located']
                                    if _address_id(path) == definition.rsplit('/', 1)[0]), None)
            if definition_path is not None:
                atom_path = definition_path[:-1] if isinstance(info['lookup'].get(definition_path[:-1]), ir.Edge) else definition_path
                parent = info['lookup'].get(atom_path[:-1])
                predicate = getattr(parent, 'where', None)
                for term in _conjuncts(predicate):
                    refs = [(path, part) for path, part in _located(predicate) if part is term]
                    if not refs or not _plain(term):
                        continue
                    relative = refs[0][0]
                    properties = [(path, part) for path, part in _located(term) if isinstance(part, ir.Property)]
                    if (properties and all(part.key != 'uid' and info['symbols'].get(_address_id(
                            (*atom_path[:-1], 'where', *relative, *path))) == sid for path, part in properties)):
                        from population import _scalar
                        if _scalar(term, family['values']) is not True:
                            return False
    return all(set(pattern.labels).issubset(family['labels'])
               and all(_compare(family['values'].get(key), '=', literal.value) is True
                       for key, literal in pattern.properties) for pattern in constraints)


def _family_reads(problem, occurrence, family, families, full):
    pattern = occurrence['pattern']
    if isinstance(pattern, ir.Node):
        if _node_can_read(occurrence, 'profile', pattern, family):
            yield {occurrence['profile']: family}
        return
    def base(term):
        return term['affine']['base'] if isinstance(term, dict) else term
    def containing(uid):
        return next((item for item in families if item['base'] <= uid < item['base'] + item['count']), None)
    for group in full.edge_groups:
        if group['type'] not in _types(pattern):
            continue
        source, target = containing(base(group['source'])), containing(base(group['target']))
        if source is not family and target is not family:
            continue
        if any(key in group['properties'] and not isinstance(group['properties'][key], dict)
               and _compare(group['properties'][key], '=', literal.value) is not True
               for key, literal in pattern.relationship.properties):
            continue
        pairs = [(source, target)]
        if pattern.direction == 'in':
            pairs = [(target, source)]
        elif pattern.direction == 'undirected':
            pairs.append((target, source))
        for left, right in pairs:
            if left is None or right is None:
                raise PopulationError('A relationship group has no proved node-family endpoints')
            if (_node_can_read(occurrence, 'left', pattern.left, left)
                    and _node_can_read(occurrence, 'right', pattern.right, right)):
                yield {occurrence['left']: left, occurrence['right']: right}


_UNKNOWN = object()


def _observer_bindings(info, observations, spec):
    interpreter = _Interpreter(spec)
    rows = [{}]
    for observation in reversed(observations):
        path = tuple(observation['query_address'])
        query = info['lookup'][path]
        if isinstance(query, (ir.Count, ir.Exists)):
            query = ir.Query(query.pattern, query.where, (ir.Literal(1),))
        parts = list(_clause_addresses(query, path))[:observation['clauses']]
        ignored = observation['ignored_where']
        if ignored is not None:
            clause, location = parts[ignored]
            options = {'where': None, **({'optional': False} if isinstance(clause, ir.Match) else {})}
            parts[ignored] = replace(clause, **options), location
        prefix = ir.Query(None, None, (ir.Literal(1),), pipeline=tuple(clause for clause, _ in parts))
        rows = [result for row in rows for result in interpreter.bindings(prefix, row)]
        if len(rows) > 4096:
            raise PopulationError('An actual row observation exceeds the finite binding budget')
    return rows


def _certify_row_demands(problem, assignment, small, full, small_families, families):
    """Certify row facts independently of masks outside their observation.

    Family copies have constant labels/maps/scalars. Prefix filters must be
    independent of changing counts or UIDs; otherwise NULL extensions or
    reachable source domains could change after the bounded proof model.
    """
    certificates = []
    problem.certified_entity_states = {}
    growth = full.counts()['nodes'] > 5000 and any(family['growing'] for family in families)

    def stable_filter(info, path, expression):
        if expression is None:
            return
        actual = _lexical_scalar(problem, info, path, expression, assignment, small, {})
        if actual is not _UNKNOWN:
            return
        def plain(path, value, seen=frozenset()):
            if isinstance(value, ir.Property):
                sid = info['symbols'].get(_address_id(path))
                return value.key != 'uid' and (info['prefix'], sid) in problem.symbol_profiles
            if isinstance(value, ir.Variable):
                sid = info['symbols'].get(_address_id(path))
                record = next((record for record in info['analysis'].symbols if record['symbol_id'] == sid), {})
                producer = record.get('producer_node')
                located = next(((p, part) for p, part in info['located'] if _address_id(p) == producer), None)
                return sid not in seen and located is not None and plain(*located, seen | {sid})
            if isinstance(value, (ir.Count, ir.Exists, ir.All)):
                return False
            if isinstance(value, ir.Literal):
                return True
            if isinstance(value, (ir.Comparison, ir.Boolean, ir.Negation, ir.Case, ir.Call)):
                return all(plain((*path, *relative), part, seen) for relative, part in _located(value)
                           if relative and isinstance(part, (ir.Property, ir.Variable, ir.Count, ir.Exists, ir.All)))
            return False
        if not plain(path, expression):
            raise PopulationError('A row observation filter is not independently stable under graph growth')

    def stable_maps(pattern):
        if not isinstance(pattern, ir.Edge):
            return
        keys = {key for key, _ in pattern.relationship.properties}
        if any(isinstance(group['properties'].get(key), dict) for group in full.edge_groups
               if group['type'] in _types(pattern) for key in keys):
            raise PopulationError('A row observation matching map has an unproved affine relationship-property dependency')

    observations = problem.row_observations()
    # The bounded row interpreter has already established these states. They
    # may discharge UID-presence tests while all observer filters and full
    # matching degrees are checked together below; no certificate is returned
    # if any producer's growth proof fails.
    for kind, info, fact in observations:
        if kind == 'binding':
            record = entity_symbol_origin(info['analysis'], fact['symbol_id'])
            problem.certified_entity_states[(info['prefix'], record['symbol_id'])] = fact['relation']
    for kind, info, fact in observations:
        if growth:
            for observer in fact['row_observers']:
                path = tuple(observer['query_address'])
                query = info['lookup'][path]
                if isinstance(query, (ir.Count, ir.Exists)):
                    query = ir.Query(query.pattern, query.where, (ir.Literal(1),))
                for index, (clause, location) in enumerate(_clause_addresses(query, path)[:observer['clauses']]):
                    if isinstance(clause, ir.Match):
                        stable_maps(clause.pattern)
                    if index != observer['ignored_where']:
                        stable_filter(info, (*location, 'where'), clause.where)
            if kind == 'matching quantity':
                path = tuple(fact['address'])
                stable_maps(info['lookup'][path])
                stable_filter(info, (*path[:-1], 'where'), getattr(info['lookup'].get(path[:-1]), 'where', None))
        if kind == 'binding':
            record = entity_symbol_origin(info['analysis'], fact['symbol_id'])
            problem.certified_entity_states[(info['prefix'], record['symbol_id'])] = fact['relation']
        certificates.append({'kind': kind, 'address': list((*info['prefix'], *fact['address'])),
                             'proof': 'actual_prefix_rows_and_growth_stable_filters' if growth else 'actual_prefix_rows_on_both_graphs',
                             'quantity_kind': fact.get('quantity_kind'), 'position': fact.get('position')})
    if full.counts()['nodes'] <= 5000:
        return certificates

    def signature(family):
        return family['profile'], family['kind'], _typed(family['labels']), _typed(family['values'])

    def containing(uid, population):
        return next((family for family in population if family['base'] <= uid < family['base'] + family['count']), None)

    def endpoint(value, index=0):
        return value['affine']['base'] + index * value['affine']['step'] if isinstance(value, dict) else value

    for demand, (_, info, fact) in zip(problem.demands, (entry for entry in observations if entry[0] == 'matching quantity')):
        occurrence = next(item for item in problem.occurrences if item['address'] == tuple(demand.address))
        pattern = occurrence['pattern']
        rows = _observer_bindings(info, fact['row_observers'], small)
        exact = problem.scale if demand.scale_matches else demand.exact_matches
        def check(value):
            if value < demand.min_matches or exact is not None and value != exact:
                raise PopulationError('An actual full-graph matching quantity differs from its logical domain degree')
        if isinstance(pattern, ir.Node):
            total = 0
            for family in families:
                if not _node_can_read(occurrence, 'profile', pattern, family):
                    continue
                truth = _lexical_scalar(problem, info, (*tuple(fact['address'])[:-1], 'where'), occurrence['predicate'], assignment, small,
                                        {occurrence['profile']: family}) if occurrence['predicate'] is not None else True
                if truth is _UNKNOWN:
                    raise PopulationError('A full-graph node matching quantity has no uniform scalar proof')
                if truth is True:
                    total += family['count']
            if rows:
                check(total)
            continue
        work = next((work for work in occurrence['works'] if demand.relationship_type is None or work['type'] == demand.relationship_type), occurrence['works'][0])
        role = 'left' if work['left'] == occurrence['left'] else 'right'
        source_node = getattr(pattern, role)
        target_node = pattern.right if role == 'left' else pattern.left
        target_role = 'right' if role == 'left' else 'left'
        target_sid = info['symbols'].get(_address_id((*tuple(fact['address']), target_role, 'variable')))
        target_definition = entity_symbol_origin(info['analysis'], target_sid)['definition']
        owner = _address_id(tuple(fact['row_observers'][0]['query_address']))
        if not target_definition.startswith(owner + '/'):
            target_profile = problem.profiles[occurrence[target_role]]
            if not target_profile.fixed or sum(family['count'] for family in families
                                               if family['profile'] == occurrence[target_role] and family['kind'] == 'valid') != 1:
                raise PopulationError('An externally bound target needs a unique row-identity degree certificate')
        sources = set()
        interpreter = _Interpreter(small)
        fresh = any(source_node.variable not in row for row in rows)
        for row in rows:
            for bound in interpreter._match(source_node, row):
                family = containing(bound[source_node.variable].uid, small_families)
                sources.add(signature(family))
        direction = pattern.direction if role == 'left' else {'in': 'out', 'out': 'in', 'undirected': 'undirected'}[pattern.direction]
        for family in families:
            if signature(family) not in sources:
                continue
            points = {family['base'], family['base'] + family['count'] - 1}
            for group in full.edge_groups:
                for value in (group['source'], group['target']):
                    for uid in (endpoint(value), endpoint(value, group['count'] - 1)):
                        points.update(uid + delta for delta in (-1, 0, 1) if family['base'] <= uid + delta < family['base'] + family['count'])
            for uid in points:
                actual = raw = 0
                for group in full.edge_groups:
                    if group['type'] != work['type']:
                        continue
                    if any(isinstance(group['properties'].get(key), dict) for key, _ in pattern.relationship.properties):
                        raise PopulationError('An affine relationship map has no exact matching-degree certificate')
                    if any(_compare(group['properties'].get(key), '=', literal.value) is not True
                           for key, literal in pattern.relationship.properties):
                        continue
                    orientations = [(group['source'], group['target'])] if direction != 'in' else [(group['target'], group['source'])]
                    if direction == 'undirected':
                        orientations.append((group['target'], group['source']))
                    for start, end in orientations:
                        if isinstance(start, dict):
                            step = start['affine']['step']
                            if step != 1:
                                raise PopulationError('A degree proof requires unit-step endpoint families')
                            index = uid - endpoint(start)
                            amount = int(0 <= index < group['count'])
                        else:
                            index, amount = 0, group['count'] if start == uid else 0
                        if not amount:
                            continue
                        raw += amount
                        target = containing(endpoint(end, index), families)
                        if target is None or not _node_can_read(occurrence, target_role, target_node, target):
                            continue
                        bindings = {occurrence[role]: family, occurrence[target_role]: target}
                        truth = _lexical_scalar(problem, info, (*tuple(fact['address'])[:-1], 'where'), occurrence['predicate'], assignment, small, bindings) if occurrence['predicate'] is not None else True
                        if truth is _UNKNOWN:
                            raise PopulationError('A full-graph edge matching quantity has no uniform scalar proof')
                        if truth is True:
                            actual += amount
                if not fresh or raw:
                    check(actual)
    return certificates


def _lexical_values(problem, info, path, expression, spec, seen=frozenset()):
    """Evaluate only the free inputs of this exact lexical expression.

    Locals introduced by COUNT/EXISTS or a WHEN branch are not outer inputs.
    Scalar aliases are closed through their producer environment. Enumerating
    physical input domains is conservative when independent aliases correlate.
    """
    records = {record['symbol_id']: record for record in info['analysis'].symbols}
    located = {_address_id(address): (address, part) for address, part in info['located']}
    environment = next((item['bindings'] for item in info['analysis']._environments
                        if item['address'] == tuple(path)), None)
    if environment is None:
        raise PopulationError('A scalar obligation has no lexical input environment')
    prefix = _address_id(path)
    used = {use['symbol_id'] for use in info['analysis'].uses
            if use['node_id'] == prefix or use['node_id'].startswith(prefix + '/')}
    def nullable(sid, visited=frozenset()):
        if sid in visited:
            return False
        record = records[sid]
        if record['kind'] == 'node':
            record = entity_symbol_origin(info['analysis'], sid)
            definition = located.get(record['definition'].rsplit('/', 1)[0])
            if definition:
                address, _ = definition
                atom = address[:-1] if isinstance(info['lookup'].get(address[:-1]), ir.Edge) else address
                parent = info['lookup'].get(atom[:-1])
                return isinstance(parent, ir.Match) and parent.optional
        return any(nullable(source, visited | {sid}) for source in record.get('sources', ()) if source in records)
    if any(nullable(sid) for sid in environment.values() if sid in used):
        observations = []
        _close_row_fact(info['tree'], tuple(path), ir.Literal(True), observations, nonempty=False)
        interpreter = _Interpreter(spec)
        return [interpreter.expression(expression, row) for row in _observer_bindings(info, observations, spec)]
    names, domains = [], []
    for name, sid in environment.items():
        if sid not in used:
            continue
        record = records[sid]
        if record['kind'] == 'node':
            record = entity_symbol_origin(info['analysis'], sid)
            definition = located.get(record['definition'].rsplit('/', 1)[0])
            node = definition[1] if definition else None
            if not isinstance(node, ir.Node):
                raise PopulationError('A scalar node input lacks a matching definition')
            values = [_Vertex(item['uid']) for item in spec.iter_nodes()
                      if set(node.labels).issubset(item['labels'])
                      and all(_compare(item['properties'].get(key), '=', literal.value) is True
                              for key, literal in node.properties)]
            values = values or [None]
        elif record['kind'] == 'scalar':
            producer = record.get('producer_node')
            if sid in seen or producer not in located:
                raise PopulationError('A scalar obligation lacks an acyclic lexical producer')
            address, value = located[producer]
            values = _lexical_values(problem, info, address, value, spec, seen | {sid})
        else:
            raise PopulationError('Relationship/path scalar inputs need an identity proof')
        names.append(name)
        domains.append(values)
    size = 1
    for domain in domains:
        size *= len(domain)
    if size > 4096:
        raise PopulationError('Scalar lexical input product exceeds the finite proof budget')
    interpreter = _Interpreter(spec)
    return [interpreter.expression(expression, dict(zip(names, values))) for values in product(*domains)]


def _stable_graph_value(problem, info, path, expression, assignment, spec):
    """A finite graph value certifies a mask only after every growth read closes."""
    if spec is _EMPTY_SPEC:
        raise PopulationError('Graph-dependent values cannot use the empty scalar proof model')
    context = getattr(problem, 'growth_context', None)
    if context is None:
        raise PopulationError('Graph-dependent masks require a complete growth context')
    families, full = context
    address = (*info['prefix'], *path)
    reads = [occurrence for occurrence in problem.occurrences
             if occurrence['address'][:len(address)] == address]
    for family in families:
        if not family['growing']:
            continue
        for occurrence in reads:
            for bindings in _family_reads(problem, occurrence, family, families, full):
                if not _local_exclusion(problem, occurrence, bindings, assignment):
                    raise PopulationError('A graph-dependent guard can observe a growing family')
    values = _lexical_values(problem, info, path, expression, spec)
    if not values or any(_typed(value) != _typed(values[0]) for value in values[1:]):
        raise PopulationError('A graph-dependent guard is not constant for its actual lexical inputs')
    return values[0]


def _lexical_scalar(problem, info, path, value, assignment, spec, bindings):
    """Resolve pure scalar WITH producers in their actual lexical scopes."""
    producers = {item['symbol_id']: item.get('producer_node') for item in info['analysis'].symbols}
    nodes = {_address_id(address): (address, part) for address, part in info['located']}
    def entity_state(sid):
        try:
            origin = entity_symbol_origin(info['analysis'], sid)['symbol_id']
        except ir.QueryError:
            return None
        return getattr(problem, 'certified_entity_states', {}).get((info['prefix'], origin))
    def resolve(sid, seen=frozenset()):
        pid = problem.symbol_profiles.get((info['prefix'], sid))
        if pid is not None:
            if sid not in seen and entity_state(sid) == 'null' and pid not in bindings:
                return None
            family = bindings.get(pid)
            if family is not None:
                return ('node_properties', [family['values']])
            record = entity_symbol_origin(info['analysis'], sid)
            definition = nodes.get(record['definition'].rsplit('/', 1)[0])
            pattern = definition[1] if definition else None
            if not isinstance(pattern, ir.Node):
                raise PopulationError('A scalar node has no proved matching definition')
            # Physical domains can overlap even for different lexical symbols.
            # A root scalar is constant only if *every* vertex available at its
            # real definition agrees on the observed value. Profile identity
            # alone cannot prove this, and UID is handled separately below.
            candidates = [node['properties'] for node in spec.iter_nodes()
                          if set(pattern.labels).issubset(node['labels'])
                          and all(_compare(node['properties'].get(key), '=', literal.value) is True
                                  for key, literal in pattern.properties)]
            if not candidates:
                raise PopulationError('An unbound scalar cannot certify an observation mask')
            return ('node_properties', candidates)
        producer = producers.get(sid)
        if sid in seen or producer not in nodes:
            raise PopulationError('No pure lexical scalar producer')
        producer_path, expression = nodes[producer]
        return evaluate(producer_path, expression, seen | {sid})
    def evaluate(address, expression, seen=frozenset()):
        try:
            return evaluate_known(address, expression, seen)
        except (PopulationError, KeyError, TypeError):
            return _UNKNOWN

    def evaluate_known(address, expression, seen=frozenset()):
        if isinstance(expression, ir.Literal):
            return expression.value
        if isinstance(expression, (ir.Property, ir.Variable)):
            sid = info['symbols'].get(_address_id(address))
            actual = resolve(sid, seen)
            if isinstance(expression, ir.Property):
                if actual is None:
                    return None
                if expression.key == 'uid' or not isinstance(actual, tuple) or actual[0] != 'node_properties':
                    raise PopulationError('An identity/relationship value is not a constant scalar certificate')
                values = [properties.get(expression.key) for properties in actual[1]]
                if any(_typed(item) != _typed(values[0]) for item in values[1:]):
                    raise PopulationError('A scalar producer has heterogeneous physical bindings')
                return values[0]
            return actual
        if isinstance(expression, ir.Comparison):
            if expression.operator in ('IS NULL', 'IS NOT NULL') and isinstance(expression.left, ir.Property) and expression.left.key == 'uid':
                sid = info['symbols'].get(_address_id((*address, 'left')))
                if entity_state(sid) == 'nonnull':
                    return expression.operator == 'IS NOT NULL'
            left = evaluate((*address, 'left'), expression.left, seen)
            if expression.operator in ('IS NULL', 'IS NOT NULL'):
                return _UNKNOWN if left is _UNKNOWN else _compare(left, expression.operator, None)
            right = evaluate((*address, 'right'), expression.right, seen)
            if left is None or right is None:
                return None
            if left is _UNKNOWN or right is _UNKNOWN:
                return _UNKNOWN
            return _compare(left, expression.operator, right)
        if isinstance(expression, ir.Boolean):
            values = [evaluate((*address, 'terms', i), term, seen) for i, term in enumerate(expression.terms)]
            decisive = False if expression.operator == 'AND' else True
            if any(value is decisive for value in values):
                return decisive
            return _UNKNOWN if any(value is _UNKNOWN for value in values) else _logical(expression.operator, values)
        if isinstance(expression, ir.Negation):
            actual = evaluate((*address, 'expression'), expression.expression, seen)
            return _UNKNOWN if actual is _UNKNOWN else None if actual is None else not actual
        if isinstance(expression, ir.Case):
            truth = evaluate((*address, 'condition'), expression.condition, seen)
            if truth is _UNKNOWN:
                return _UNKNOWN
            field = 'then' if truth is True else 'otherwise'
            return evaluate((*address, field), getattr(expression, field), seen)
        if isinstance(expression, ir.Call) and expression.name == 'coalesce':
            for i, term in enumerate(expression.args):
                actual = evaluate((*address, 'args', i), term, seen)
                if actual is not None:
                    return actual
            return None
        if isinstance(expression, (ir.Count, ir.Exists)):
            return _stable_graph_value(problem, info, address, expression, assignment, spec)
        raise PopulationError('A graph-dependent value cannot certify a scalar mask')
    return evaluate(path, value)


def _certify_value_demands(problem, assignment, small):
    """Each scalar fact must survive growth on its own observation domain.

    An outer query mask cannot justify an inner COUNT equality. Conversely,
    a non-NULL type fact need not freeze a COUNT's changing integer value.
    """
    if not any(family['growing'] for family in problem.growth_context[0]):
        return []
    certificates = []
    for obligation in problem.obligations:
        if not isinstance(obligation, ValueDemand):
            continue
        address = tuple(obligation.address)
        info = next(info for info in problem.infos if address[:len(info['prefix'])] == info['prefix'])
        path = address[len(info['prefix']):]

        def nonnullable(path, seen=frozenset()):
            value = info['lookup'][path]
            if isinstance(value, ir.Variable):
                sid = info['symbols'].get(_address_id(path))
                record = next((record for record in info['analysis'].symbols if record['symbol_id'] == sid), {})
                producer = record.get('producer_node')
                location = next((path for path, _ in info['located'] if _address_id(path) == producer), None)
                return sid not in seen and location is not None and nonnullable(location, seen | {sid})
            if isinstance(value, ir.Case):
                return nonnullable((*path, 'then'), seen) and nonnullable((*path, 'otherwise'), seen)
            if isinstance(value, ir.Call) and value.name == 'coalesce':
                return any(nonnullable((*path, 'args', i), seen) for i in range(len(value.args)))
            from dependencies import _nonnullable
            return _nonnullable(value)

        typed_nonnull = obligation.relation == 'nonnull' and nonnullable(path)
        actual = _lexical_scalar(problem, info, path, info['lookup'][path], assignment, small, {})
        if not typed_nonnull and (actual is _UNKNOWN
                or (actual is None if obligation.relation == 'nonnull' else _typed(actual) != _typed(obligation.value))):
            raise PopulationError('A scalar obligation is not independently stable under graph growth')
        certificates.append({'address': list(address), 'relation': obligation.relation,
                             'proof': 'nonnull_scalar_type' if typed_nonnull else 'constant_scalar_under_growth'})
    return certificates


def _local_exclusion(problem, occurrence, bindings, assignment):
    info = occurrence['info']
    local = occurrence['address'][len(info['prefix']):]
    for term in problem._local_terms(occurrence):
        # Find the exact term address, avoiding structural equality when a
        # source AST reuses the same immutable expression at several sites.
        addresses = [(*local[:-1], 'where', *path) for path, value in _located(occurrence['predicate']) if value is term]
        if not addresses:
            continue
        actual = _lexical_scalar(problem, info, addresses[0], term, assignment, _EMPTY_SPEC, bindings)
        if actual is False or actual is None:
            return True
    return False


_EMPTY_SPEC = GraphSpec('AST', 1, 0, version=2, include_reference=False,
                       node_groups=({'count': 1, 'labels': ['GspScalarModel'], 'properties': {'uid': 1}},),
                       edge_groups=(), index_requirements=())


def _masking_ancestor(problem, occurrence, assignment, spec, bindings):
    address = occurrence['address']
    info = occurrence['info']
    prefix = info['prefix']
    local = address[len(prefix):]
    def scalar(path, value):
        return _lexical_scalar(problem, info, path, value, assignment, spec, bindings)
    for length in range(len(local)):
        ancestor = info['lookup'].get(local[:length])
        child = local[length]
        kind = type(ancestor).__name__
        if kind == 'RowLimit' and ancestor.limit == 0:
            return {'kind': 'zero_limit', 'address': list((*prefix, *local[:length]))}
        if isinstance(ancestor, ir.Case):
            truth = scalar((*local[:length], 'condition'), ancestor.condition)
            selected = 'then' if truth is True else 'otherwise' if truth is False or truth is None else None
            if child in ('then', 'otherwise') and selected is not None and child != selected:
                return {'kind': 'case_selected_other_branch', 'address': list((*prefix, *local[:length])), 'condition': truth}
        if kind == 'ConditionalQuery':
            truth = scalar((*local[:length], 'condition'), ancestor.condition)
            selected = 'then' if truth is True else 'otherwise' if truth is False or truth is None else None
            if child in ('then', 'otherwise') and selected is not None and child != selected:
                return {'kind': 'conditional_selected_other_branch', 'address': list((*prefix, *local[:length])), 'condition': truth}
        if isinstance(ancestor, ir.Boolean) and child == 'terms':
            index = local[length + 1]
            decisive = False if ancestor.operator == 'AND' else True
            if any(i != index and scalar((*local[:length], 'terms', i), term) is decisive for i, term in enumerate(ancestor.terms)):
                return {'kind': 'decisive_boolean_sibling', 'address': list((*prefix, *local[:length])), 'value': decisive}
        if isinstance(ancestor, ir.Comparison) and ancestor.operator not in ('IS NULL', 'IS NOT NULL'):
            sibling = 'right' if child == 'left' else 'left' if child == 'right' else None
            if sibling is not None and scalar((*local[:length], sibling), getattr(ancestor, sibling)) is None:
                return {'kind': 'null_comparison_operand', 'address': list((*prefix, *local[:length]))}
        if isinstance(ancestor, ir.Call) and ancestor.name == 'coalesce' and child == 'args':
            index = local[length + 1]
            if any((value := scalar((*local[:length], 'args', i), term)) is not None and value is not _UNKNOWN
                   for i, term in enumerate(ancestor.args[:index])):
                return {'kind': 'nonnull_coalesce_prefix', 'address': list((*prefix, *local[:length]))}
        if isinstance(ancestor, ir.Query):
            # A literal/scalar false row guard removes every row from this
            # query, including expansions whose expression is never observed.
            if ancestor.pipeline:
                clauses = [((*local[:length], 'pipeline', i, 'where'), clause)
                           for i, clause in enumerate(ancestor.pipeline)]
            else:
                clauses = [((*local[:length], 'where'), ir.Match(ancestor.pattern, ancestor.where))]
                clauses.extend(((*local[:length], 'matches', i, 'where'), clause)
                               for i, clause in enumerate(ancestor.matches))
            for where_path, clause in clauses:
                if isinstance(clause, ir.Match) and not clause.optional and clause.where is not None:
                    truth = scalar(where_path, clause.where)
                    if truth is False or truth is None:
                        return {'kind': 'false_row_guard', 'address': list((*prefix, *local[:length]))}
    return None
