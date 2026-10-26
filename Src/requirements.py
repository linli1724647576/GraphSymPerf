"""Inward semantic requirements and bounded cardinality allocation.

The inputs describe observable output and potential work. Matching quantities
are a result of lowering those goals through the actual typed AST, not a
pattern-specific topology or an attachment of exact counts to hand-picked edges.
"""

from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import count, product
from math import ceil, prod

import ir
from dependencies import analyze_dependencies, node_at, _clause_addresses
from population import (MatchDemand, PathDemand, PopulationError, ValueDemand, EntityBindingDemand,
                        _located, _symbol_lookup, _compare)


_VALUES = (False, True, None)
_BRANCH_BUDGET = 256
_PLAN_BUDGET = 256
_ALLOCATIONS_PER_BRANCH = 32


class _ChoiceRequired(Exception):
    def __init__(self, width, reason):
        self.width, self.reason = width, reason


def _logical(operator, values):
    decisive = False if operator == 'AND' else True
    if any(value is decisive for value in values):
        return decisive
    return None if any(value is None for value in values) else not decisive


def _intervals_intersection(left, right):
    result = []
    for lower_a, upper_a in left:
        for lower_b, upper_b in right:
            lower = max(lower_a, lower_b, 0)
            upper = upper_b if upper_a is None else upper_a if upper_b is None else min(upper_a, upper_b)
            if upper is None or lower <= upper:
                result.append((lower, upper))
    return tuple(sorted(set(result), key=lambda interval: (interval[0], float('inf') if interval[1] is None else interval[1])))


@dataclass(frozen=True)
class RequirementPlan:
    demands: tuple[MatchDemand | PathDemand, ...]
    obligations: tuple[ValueDemand, ...]
    evidence: dict


@dataclass(frozen=True)
class _Bound:
    factors: tuple
    minimum: int = 0
    maximum: int | None = None
    address: tuple = ()


class _Lowering:
    def __init__(self, source, alternative, correspondence=(), decisions=()):
        self.trees = {('query',): source, ('alternatives', 0): alternative}
        self.infos = {}
        self.variables, self.atoms, self.bounds, self.values = {}, {}, [], {}
        self.nonnulls, self.visited_values = {}, set()
        self.scalar_values, self.scalar_nonnulls = {}, set()
        self.match_states, self.entities, self._active_entities = {}, {}, set()
        self.steps = []
        self.count_domains = {}
        self.path_domains, self.path_truths = {}, {}
        self.decisions, self._decision_tape = [], tuple(decisions)
        self.origins = {}
        self.active_path = ()
        self.visited_rows, self.visited_truth = set(), set()
        for prefix, tree in self.trees.items():
            analysis = analyze_dependencies(tree)
            located = list(_located(tree))
            self.infos[prefix] = {
                'analysis': analysis, 'lookup': dict(located),
                'symbols': _symbol_lookup(analysis),
                'records': {record['symbol_id']: record for record in analysis.symbols},
                'locations': {self.node_id(path): (path, item) for path, item in located},
            }
        for relation in correspondence:
            if not relation.equal:
                continue
            left, right = tuple(relation.left_address), tuple(relation.right_address)
            if left[:1] == ('query',) and right[:2] == ('alternatives', 0):
                source_path, target_path = left[1:], right[2:]
            elif right[:1] == ('query',) and left[:2] == ('alternatives', 0):
                source_path, target_path = right[1:], left[2:]
            else:
                continue
            source_sid = self._sid(('query',), (*source_path, 'variable'))
            target_sid = self._sid(('alternatives', 0), (*target_path, 'variable'))
            origin = (('query',), source_sid)
            target = (('alternatives', 0), target_sid)
            if target in self.origins and self.origins[target] != origin:
                raise PopulationError('A copied node has conflicting lexical origins')
            self.origins[target] = origin

    def node_producer(self, prefix, path):
        sid = self._sid(prefix, (*path, 'variable'))
        origin_prefix, origin_sid = prefix, self.entity_symbol(prefix, sid)
        record = self.infos[origin_prefix]['records'][origin_sid]
        definition = self.infos[origin_prefix]['locations'].get(record['definition'])
        if definition is None or definition[0][-1:] != ('variable',):
            raise PopulationError('An entity requirement has no matching definition')
        node_path = definition[0][:-1]
        for query_path, query in self.infos[origin_prefix]['lookup'].items():
            if not isinstance(query, ir.Query):
                continue
            for index, (clause, location) in enumerate(_clause_addresses(query, query_path)):
                if not isinstance(clause, ir.Match):
                    continue
                pattern_path = (*location, 'pattern')
                paths = (pattern_path,) if isinstance(clause.pattern, ir.Node) else ((*pattern_path, 'left'), (*pattern_path, 'right'))
                if node_path in paths:
                    return origin_prefix, node_path, query_path, index, clause, location
        raise PopulationError('An entity producer needs a supported matching clause')

    def match_state(self, prefix, clause, location, desired=None):
        key = self.atom(prefix, (*location, 'pattern'))
        if key is None:
            if clause.optional:
                raise PopulationError('An OPTIONAL without a fresh matching domain needs a binding-filter transfer rule')
            return True
        if desired is None:
            if key in self.match_states:
                return self.match_states[key]
            desired = self.choose((False, True), 'OPTIONAL zero-or-positive matching state')
        if key in self.match_states and self.match_states[key] is not desired:
            raise PopulationError('Conflicting zero/positive matching and entity-binding requirements')
        self.match_states[key] = desired
        self.bind((key,), 1 if desired else 0, None if desired else 0, (*prefix, *location))
        return desired

    def entity(self, prefix, path, nonnull, *, export=True):
        path = tuple(path)
        address = (*prefix, *path)
        relation = 'nonnull' if nonnull else 'null'
        if address in self.entities and self.entities[address] != relation:
            raise PopulationError('Conflicting entity NULL/non-NULL requirements')
        if export:
            self.entities[address] = relation
        token = (prefix, path, nonnull)
        if token in self._active_entities:
            return
        self._active_entities.add(token)
        origin, node_path, query_path, index, clause, location = self.node_producer(prefix, path)
        if not clause.optional and not nonnull:
            raise PopulationError('A mandatory entity binding cannot be NULL on a nonempty output domain')
        if clause.optional:
            self.match_state(origin, clause, location, nonnull)
        elif nonnull:
            key = self.atom(origin, (*location, 'pattern'))
            self.bind((key,), 1, address=(*origin, *location))
        if nonnull:
            pattern_path = (*location, 'pattern')
            nodes = (pattern_path,) if isinstance(clause.pattern, ir.Node) else ((*pattern_path, 'left'), (*pattern_path, 'right'))
            for imported_path in nodes:
                sid = self._sid(origin, (*imported_path, 'variable'))
                record = self.infos[origin]['records'][sid]
                if record['definition'] != self.node_id((*imported_path, 'variable')):
                    self.entity(origin, imported_path, True)
        self.steps.append({'operator': 'EntityBinding', 'address': list(address), 'relation': relation,
                           'requirement': 'after_match_binding_domain', 'minimum_input_rows': 1,
                           'producer': list((*origin, *location))})

    def property_entity(self, prefix, path, nonnull, *, enforce=True):
        sid = self._sid(prefix, path)
        origin, sid = prefix, self.entity_symbol(prefix, sid)
        record = self.infos[origin]['records'][sid]
        definition = self.infos[origin]['locations'].get(record['definition'])
        if definition is None:
            raise PopulationError('A property input has no actual entity producer')
        _, _, _, _, producer, _ = self.node_producer(origin, definition[0][:-1])
        if nonnull and not producer.optional:
            # Mandatory bindings are non-NULL on their output domain by
            # semantics. Re-exporting this tautology for every scalar leaf
            # would add unnecessary observation-domain obligations.
            return
        if not enforce:
            _, _, _, _, _, location = self.node_producer(origin, definition[0][:-1])
            if origin == prefix and tuple(path)[:len((*location, 'where'))] == (*location, 'where'):
                # A local rejected-witness choice may have no matching rows;
                # it cannot introduce a non-vacuous entity-domain fact.
                return
        self.entity(origin, definition[0][:-1], nonnull)

    def nullable_import(self, prefix, node_path):
        origin, _, _, _, producer, location = self.node_producer(prefix, node_path)
        return producer.optional and not self.match_state(origin, producer, location)

    def row_factor(self, prefix, clause, location):
        """A non-NULL input yields m rows, or max(1,m) for OPTIONAL."""
        factor = self.atom(prefix, (*location, 'pattern'))
        pattern_path = (*location, 'pattern')
        nodes = (pattern_path,) if isinstance(clause.pattern, ir.Node) else ((*pattern_path, 'left'), (*pattern_path, 'right'))
        null_input = False
        for node_path in nodes:
            sid = self._sid(prefix, (*node_path, 'variable'))
            if self.infos[prefix]['records'][sid]['definition'] != self.node_id((*node_path, 'variable')):
                null_input |= self.nullable_import(prefix, node_path)
        if null_input:
            if factor is None:
                raise PopulationError('A NULL imported binding needs an explicit matching domain')
            self.match_state(prefix, clause, location, False)
        if clause.optional and clause.where is not None:
            known, value = self.known_scalar(prefix, (*location, 'where'))
            if known and value is not True:
                self.match_state(prefix, clause, location, False)
        positive = self.match_state(prefix, clause, location) if clause.optional else not null_input
        if clause.optional:
            self.steps.append({'operator': 'OptionalMatch', 'address': list((*prefix, *location)),
                               'requirement': 'per_input_row_transfer', 'matching_state': 'positive' if positive else 'zero',
                               'transfer': 'max(1,m)', 'selected_transfer': 'm' if positive else '1',
                               'new_bindings': 'nonnull' if positive else 'null'})
            return factor if positive else None
        return factor

    def choose(self, alternatives, reason):
        alternatives = tuple(alternatives)
        if not alternatives:
            raise PopulationError(f'No candidate satisfies {reason}')
        if len(alternatives) == 1:
            return alternatives[0]
        index = len(self.decisions)
        if index == len(self._decision_tape):
            raise _ChoiceRequired(len(alternatives), reason)
        selected = self._decision_tape[index]
        if not 0 <= selected < len(alternatives):
            raise PopulationError('An inward decision is outside its candidate domain')
        self.decisions.append({'reason': reason, 'candidate': selected, 'candidates': len(alternatives)})
        return alternatives[selected]

    def entity_symbol(self, prefix, sid):
        seen = set()
        while sid not in seen:
            seen.add(sid)
            record = self.infos[prefix]['records'].get(sid, {})
            location = self.infos[prefix]['locations'].get(record.get('producer_node'))
            if record.get('kind') != 'node' or location is None or not isinstance(location[1], ir.Variable):
                return sid
            sid = self.infos[prefix]['symbols'].get(self.node_id(location[0]))
            if sid is None:
                break
        raise PopulationError('An entity alias lacks an acyclic lexical source binding')

    def canonical(self, prefix, sid):
        seen = set()
        while True:
            token = self.origins.get((prefix, sid), (prefix, sid))
            if token in seen:
                raise PopulationError('A cyclic entity producer cannot define a binding domain')
            seen.add(token)
            prefix, sid = token
            record = self.infos[prefix]['records'].get(sid, {})
            location = self.infos[prefix]['locations'].get(record.get('producer_node'))
            if record.get('kind') != 'node' or location is None or not isinstance(location[1], ir.Variable):
                return token
            source = self.infos[prefix]['symbols'].get(self.node_id(location[0]))
            if source is None or source == sid:
                raise PopulationError('An entity alias lacks a distinct lexical source binding')
            sid = source

    @staticmethod
    def node_id(path):
        return 'root' + ''.join('/' + str(part) for part in path)

    def value(self, prefix, path):
        return self.infos[prefix]['lookup'][tuple(path)]

    def _producer(self, prefix, path, expression):
        if not isinstance(expression, ir.Variable):
            return tuple(path), expression
        info = self.infos[prefix]
        sid = info['symbols'].get(self.node_id(path))
        record = info['records'].get(sid, {})
        producer = record.get('producer_node')
        # A bare RETURN variable is both an input use and an exported scalar
        # definition at this address. Follow its explicit source edge rather
        # than mistaking the export's self-address for a cyclic producer.
        followed = set()
        while producer == self.node_id(path) and len(record.get('sources', ())) == 1:
            source = record['sources'][0]
            if source in followed:
                raise PopulationError('A cyclic scalar export cannot carry an inward demand')
            followed.add(source)
            record = info['records'].get(source, {})
            producer = record.get('producer_node')
        if producer not in info['locations']:
            raise PopulationError('An inward scalar demand has no lexical producer')
        result = info['locations'][producer]
        if result[0] == tuple(path):
            raise PopulationError('A cyclic scalar producer cannot carry an inward demand')
        return self._producer(prefix, *result)

    def _sid(self, prefix, path):
        result = self.infos[prefix]['symbols'].get(self.node_id(path))
        if result is None:
            raise PopulationError('A cardinality atom has no lexical node binding')
        return result

    def atom(self, prefix, path):
        """Return the per-input matching factor for one actual occurrence."""
        path = tuple(path)
        address = (*prefix, *path)
        if address in self.atoms:
            return self.atoms[address]
        pattern = self.value(prefix, path)
        if isinstance(pattern, ir.Edge):
            left = self.canonical(prefix, self._sid(prefix, (*path, 'left', 'variable')))
            right = self.canonical(prefix, self._sid(prefix, (*path, 'right', 'variable')))
            bounded = pattern.relationship.max_hops is not None
            if bounded:
                relation = pattern.relationship
                if (pattern.direction != 'undirected' or not pattern.path_variable
                        or type(relation.min_hops) is not int or type(relation.max_hops) is not int
                        or not 1 <= relation.min_hops <= relation.max_hops <= 8
                        or relation.path_mode not in ('trail', 'acyclic')):
                    raise PopulationError('A bounded path needs an undirected TRAIL/ACYCLIC binding with bounds in 1..8')
                # Mirroring changes syntax order, not the per-binding path
                # population. Lexical endpoint origins align the two copies.
                endpoints = tuple(sorted((left, right), key=repr))
                key = ('path', endpoints, relation)
                self.path_domains.setdefault(key, ((relation.min_hops, relation.max_hops),))
                self.path_truths.setdefault(key, {})
            else:
                if pattern.relationship.min_hops is not None or pattern.path_variable:
                    raise PopulationError('An unbounded or unnamed path has no finite quantity rule')
                key = ('edge', left, right, pattern.direction, pattern.relationship)
            self.variables.setdefault(key, {'addresses': [], 'kind': 'path' if bounded else 'edge', 'left': left, 'right': right,
                                            'prefix': prefix, 'pattern': pattern})
        elif isinstance(pattern, ir.Node):
            sid = self._sid(prefix, (*path, 'variable'))
            record = self.infos[prefix]['records'][sid]
            # A fixed or already imported node is one input binding, not a
            # fresh scan whose population may be enlarged to pass a filter.
            if pattern.properties or record['definition'] != self.node_id((*path, 'variable')):
                self.atoms[address] = None
                return None
            endpoint = self.canonical(prefix, sid)
            key = ('node', endpoint)
            self.variables.setdefault(key, {'addresses': [], 'kind': 'node', 'left': None, 'right': endpoint,
                                            'prefix': prefix, 'pattern': pattern})
        else:
            raise PopulationError('A row requirement must address a matching Node or Edge')
        if address not in self.variables[key]['addresses']:
            self.variables[key]['addresses'].append(address)
        self.atoms[address] = key
        return key

    def path_atom(self, prefix, argument_path):
        """Resolve a path use by its actual lexical producer, including aliases."""
        info = self.infos[prefix]
        sid = self._sid(prefix, argument_path)
        seen = set()
        while sid not in seen:
            seen.add(sid)
            record = info['records'].get(sid, {})
            definition = info['locations'].get(record.get('definition'))
            if definition is not None and definition[0][-1:] == ('path_variable',):
                location = definition[0][:-1]
                if isinstance(self.value(prefix, location), ir.Edge):
                    return self.atom(prefix, location), location
            producer = info['locations'].get(record.get('producer_node'))
            if producer is None or not isinstance(producer[1], ir.Variable):
                break
            sid = info['symbols'].get(self.node_id(producer[0]))
        raise PopulationError('A path observation has no acyclic lexical bounded-path producer')

    def path_length(self, prefix, path, expression):
        if not (isinstance(expression, ir.Call) and expression.name == 'length'
                and len(expression.args) == 1 and isinstance(expression.args[0], ir.Variable)):
            return None
        sid = self.infos[prefix]['symbols'].get(self.node_id((*path, 'args', 0)))
        if self.infos[prefix]['records'].get(sid, {}).get('kind') != 'path':
            return None
        return self.path_atom(prefix, (*path, 'args', 0))[0]

    def length_interval(self, prefix, path, expression, intervals):
        key = self.path_length(prefix, path, expression)
        if key is None:
            raise PopulationError('A length requirement must consume an actual path binding')
        domain = _intervals_intersection(self.path_domains[key], intervals)
        if not domain:
            raise PopulationError('Path length interval intersection is empty')
        self.path_domains[key] = domain
        self.steps.append({'operator': 'PathLength', 'address': list((*prefix, *path)),
                           'requirement': 'integer_domain', 'intervals': [list(item) for item in domain]})
        selected = self.choose(domain, 'path length interval-union branch')
        self.path_domains[key] = (selected,)

    def all_truth(self, prefix, path, expression, desired):
        if not (isinstance(expression.items, ir.Call) and expression.items.name == 'nodes'
                and len(expression.items.args) == 1 and isinstance(expression.items.args[0], ir.Variable)):
            raise PopulationError('ALL requirements need node-local values over a bounded path')
        key, location = self.path_atom(prefix, (*path, 'items', 'args', 0))
        if desired not in self.possible(prefix, (*path, 'predicate')):
            raise PopulationError('The nonempty path node domain contradicts the required ALL truth')
        facts = self.path_truths[key]
        address = (*prefix, *path)
        if address in facts and facts[address] is not desired:
            raise PopulationError('Conflicting TRUE/FALSE/NULL requirements for one path ALL')
        facts[address] = desired
        self.steps.append({'operator': 'All', 'address': list(address), 'requirement': 'path_node_truth',
                           'value': desired, 'path_producer': list((*prefix, *location)),
                           'transfer': ('all_nodes_true' if desired is True else 'at_least_one_false'
                                        if desired is False else 'no_false_and_at_least_one_null'),
                           'includes_endpoints': True})

    def bind(self, factors, minimum=0, maximum=None, address=()):
        if maximum is not None and (maximum < 0 or minimum > maximum):
            raise PopulationError('Incompatible inward row-count bounds')
        factors = tuple(key for key in factors if key is not None)
        if not factors and (minimum > 1 or maximum is not None and maximum < 1):
            raise PopulationError('A fixed input row cannot satisfy the requested outer cardinality')
        bound = _Bound(factors, minimum, maximum, tuple(address))
        if bound not in self.bounds:
            self.bounds.append(bound)

    def rows(self, prefix, path, minimum=0, maximum=None):
        path = tuple(path)
        token = (prefix, path, minimum, maximum)
        if token in self.visited_rows:
            return
        self.visited_rows.add(token)
        flow = self.value(prefix, path)
        self.steps.append({'operator': type(flow).__name__, 'address': list((*prefix, *path)),
                           'requirement': 'rows', 'minimum': minimum, 'maximum': maximum})
        if isinstance(flow, ir.RowLimit):
            if flow.limit < minimum:
                raise PopulationError('An enclosing LIMIT cannot supply the requested row count')
            self.rows(prefix, (*path, 'body'), minimum, maximum if maximum is not None and maximum < flow.limit else None)
            return
        if isinstance(flow, ir.ConditionalQuery):
            branch = None
            if prefix == ('query',) and self.active_path[:len(path)] == path and len(self.active_path) > len(path):
                child = self.active_path[len(path)]
                if child in ('then', 'otherwise'):
                    branch = child
            values = self.possible(prefix, (*path, 'condition'))
            choices = [(value, 'then' if value is True else 'otherwise') for value in (True, False, None)
                       if value in values and (branch is None or ('then' if value is True else 'otherwise') == branch)]
            desired, branch = self.choose(choices, 'conditional selected row branch')
            self.truth(prefix, (*path, 'condition'), desired)
            self.rows(prefix, (*path, branch), minimum, maximum)
            return
        if isinstance(flow, ir.UnionAll):
            # One branch can supply a positive lower bound. An exact upper
            # bound also requires the other branches to contribute no rows.
            selected = self.choose(range(len(flow.branches)), 'UNION row supplier')
            self.rows(prefix, (*path, 'branches', selected), minimum, maximum)
            if maximum is not None:
                for index in range(len(flow.branches)):
                    if index != selected:
                        self.rows(prefix, (*path, 'branches', index), 0, 0)
            return
        if not isinstance(flow, ir.Query):
            raise PopulationError('Unsupported row-stream operator for inward requirements')
        if flow.limit is not None and flow.limit < minimum:
            raise PopulationError('A query LIMIT is smaller than its required output')
        if flow.distinct and minimum > 1:
            raise PopulationError('A DISTINCT cardinality demand needs a distinct-value construction rule')
        parts = _clause_addresses(flow, path)
        if minimum > 1 and any(isinstance(part, ir.With) and part.distinct for part, _ in parts):
            # Nonemptiness and emptiness survive duplicate elimination, but a
            # product of matching populations is not a lower bound on the
            # number of distinct projected tuples.
            raise PopulationError('A WITH DISTINCT cardinality demand needs a distinct-value construction rule')
        factors, filters = [], []
        for part, location in parts:
            if isinstance(part, ir.Match):
                factor = self.row_factor(prefix, part, location)
                if factor is not None:
                    factors.append(factor)
            if part.where is not None and (not isinstance(part, ir.Match) or not part.optional
                                          or self.match_state(prefix, part, location)):
                filters.append((*location, 'where'))
        if maximum == 0 and filters:
            # Empty input is a semantic option, not deletion of a keyed root.
            # This is a witness-construction choice inside the row producer.
            # The enclosing Exists/Count fact observes emptiness; its local
            # candidate variable is not a universal outer-state obligation.
            choices = [(location, value) for location in filters for value in (False, None)
                       if value in self.possible(prefix, location)]
            # A fresh matching domain may also be empty while its predicate
            # remains unchanged. This is essential when a protected work
            # family adds rejected candidates but no successful match.
            if factors:
                choices.append((None, None))
            location, value = self.choose(choices, 'non-TRUE local row filter')
            if location is None:
                self.bind(factors, 0, 0, (*prefix, *path))
            else:
                self.truth(prefix, location, value, enforce=False)
            return
        if minimum > 0:
            for location in filters:
                self.truth(prefix, location, True, enforce=False)
        self.bind(factors, minimum, maximum, (*prefix, *path))

    def _subquery_rows(self, prefix, path, expression, minimum, maximum=None):
        if expression.body is not None:
            self.rows(prefix, (*path, 'body'), minimum, maximum)
        else:
            factor = self.atom(prefix, (*path, 'pattern'))
            if expression.where is not None and minimum > 0:
                self.truth(prefix, (*path, 'where'), True, enforce=False)
            self.bind((factor,), minimum, maximum, (*prefix, *path))

    def activate_mark(self):
        """Make the marked local envelope observable through its context."""
        prefix = ('query',)
        for length in range(len(self.active_path)):
            path = self.active_path[:length]
            ancestor = self.value(prefix, path)
            child = self.active_path[length]
            if isinstance(ancestor, ir.Query):
                self.query_observer_inputs(prefix, path, self.active_path)
            if isinstance(ancestor, ir.RowLimit) and ancestor.limit == 0:
                raise PopulationError('A marked observer cannot be reached below a zero row limit')
            if isinstance(ancestor, (ir.ConditionalQuery, ir.Case)) and child in ('then', 'otherwise'):
                options = [value for value in (True, False, None)
                           if value in self.possible(prefix, (*path, 'condition'))
                           and ('then' if value is True else 'otherwise') == child]
                desired = self.choose(options, 'marked conditional ancestor activation')
                self.truth(prefix, (*path, 'condition'), desired)
            elif isinstance(ancestor, ir.Boolean) and child == 'terms':
                index = self.active_path[length + 1]
                for other in range(len(ancestor.terms)):
                    if other != index:
                        self.truth(prefix, (*path, 'terms', other), ancestor.operator == 'AND')
            elif isinstance(ancestor, ir.Call) and ancestor.name == 'coalesce' and child == 'args':
                for before in range(self.active_path[length + 1]):
                    self.null(prefix, (*path, 'args', before))

    @staticmethod
    def _intervals(operator, literal, truth):
        if type(literal) is not int or truth is None:
            return ()
        if truth is False:
            operator = {'>': '<=', '>=': '<', '<': '>=', '<=': '>', '=': '<>', '<>': '='}.get(operator, operator)
        intervals = {'>': ((literal + 1, None),), '>=': ((literal, None),),
                     '<': ((0, literal - 1),), '<=': ((0, literal),),
                     '=': ((literal, literal),), '<>': ((0, literal - 1), (literal + 1, None))}.get(operator, ())
        return _intervals_intersection(intervals, ((0, None),))

    def possible(self, prefix, path):
        """Conservative three-valued output domain used only to prune choices."""
        address = (*prefix, *tuple(path))
        if address in self.values:
            known = self.values[address]
            return (known,) if known is None or type(known) is bool else ()
        path, expression = self._producer(prefix, tuple(path), self.value(prefix, path))
        if isinstance(expression, ir.Literal):
            return (expression.value,) if expression.value is None or type(expression.value) is bool else ()
        if isinstance(expression, ir.Exists):
            return (False, True)
        if isinstance(expression, ir.Count):
            return ()  # Counts are non-null integers, never Boolean values.
        if self.path_length(prefix, path, expression) is not None:
            return ()  # A bound path length is a non-null integer.
        if isinstance(expression, ir.All):
            # nodes(path) contains both endpoints, so this supported
            # positive-length domain cannot gain vacuous TRUE from [].
            return self.possible(prefix, (*path, 'predicate'))
        if isinstance(expression, ir.Negation):
            return tuple(None if value is None else not value for value in self.possible(prefix, (*path, 'expression')))
        if isinstance(expression, ir.Boolean):
            domains = [self.possible(prefix, (*path, 'terms', i)) for i in range(len(expression.terms))]
            result = (expression.operator == 'AND',)
            for domain in domains:
                values = [_logical(expression.operator, pair) for pair in product(result, domain)]
                result = tuple(value for value in _VALUES if value in values)
            return result
        if isinstance(expression, ir.Comparison):
            left_path, left = self._producer(prefix, (*path, 'left'), expression.left)
            right_path, right = self._producer(prefix, (*path, 'right'), expression.right)
            if expression.operator in ('IS NULL', 'IS NOT NULL'):
                return (False, True)
            if (isinstance(left, ir.Literal) and left.value is None
                    or isinstance(right, ir.Literal) and right.value is None):
                return (None,)
            if isinstance(left, ir.Count) and isinstance(right, ir.Literal) or isinstance(right, ir.Count) and isinstance(left, ir.Literal):
                return (False, True)
            return _VALUES
        if isinstance(expression, ir.Case):
            condition = self.possible(prefix, (*path, 'condition'))
            domains = []
            if True in condition:
                domains.extend(self.possible(prefix, (*path, 'then')))
            if False in condition or None in condition:
                domains.extend(self.possible(prefix, (*path, 'otherwise')))
            return tuple(value for value in _VALUES if value in domains)
        return _VALUES

    def _fact(self, prefix, path, expected, relation, enforce):
        address = (*prefix, *path)
        if not enforce:
            return
        producer_path, producer = self._producer(prefix, tuple(path), self.value(prefix, path))
        if isinstance(producer, ir.Property):
            sid = self.infos[prefix]['symbols'].get(self.node_id(producer_path))
            identity = (self.canonical(prefix, sid), producer.key)
            # Scalar facts on transported bindings must share one domain.
            # Alias spelling and source/alternative occurrence addresses do
            # not create independent values for the same physical property.
            if relation == 'nonnull':
                if identity in self.scalar_values and self.scalar_values[identity] is None:
                    raise PopulationError('A transported scalar nonnull requirement conflicts with exact NULL')
                self.scalar_nonnulls.add(identity)
            else:
                if identity in self.scalar_values and (type(self.scalar_values[identity]) is not type(expected)
                                                       or self.scalar_values[identity] != expected):
                    raise PopulationError('Conflicting typed values for one transported scalar property')
                if expected is None and identity in self.scalar_nonnulls:
                    raise PopulationError('A transported scalar NULL requirement conflicts with nonnull')
                self.scalar_values[identity] = expected
        if relation == 'nonnull':
            if address in self.values and self.values[address] is None:
                raise PopulationError('A nonnull requirement contradicts an exact NULL value')
            self.nonnulls[address] = None
        else:
            if address in self.values and (type(self.values[address]) is not type(expected) or self.values[address] != expected):
                raise PopulationError('Conflicting typed value requirements at one lexical expression')
            if expected is None and address in self.nonnulls:
                raise PopulationError('An exact NULL value contradicts a nonnull requirement')
            self.values[address] = expected

    def can_equal(self, prefix, path, expected):
        address = (*prefix, *path)
        if address in self.values:
            return type(self.values[address]) is type(expected) and self.values[address] == expected
        path, expression = self._producer(prefix, tuple(path), self.value(prefix, path))
        if isinstance(expression, ir.Literal):
            return type(expression.value) is type(expected) and expression.value == expected
        if isinstance(expression, ir.Count):
            return type(expected) is int and expected >= 0
        if self.path_length(prefix, path, expression) is not None:
            return type(expected) is int and 1 <= expected <= 8
        if isinstance(expression, (ir.Exists, ir.Boolean, ir.Comparison, ir.Negation, ir.All)):
            return (expected is None or type(expected) is bool) and expected in self.possible(prefix, path)
        if isinstance(expression, ir.Case):
            return any(self.can_equal(prefix, (*path, branch), expected) for branch in ('then', 'otherwise'))
        if isinstance(expression, ir.Call) and expression.name == 'coalesce':
            return (all(self.can_equal(prefix, (*path, 'args', i), None) for i in range(len(expression.args)))
                    if expected is None else any(self.can_equal(prefix, (*path, 'args', i), expected)
                                                for i in range(len(expression.args))))
        return isinstance(expression, ir.Property)

    def require_value(self, prefix, path, expected, *, relation='equal', enforce=True):
        """Lower an exact typed scalar or non-NULL fact through its producer."""
        if relation == 'nonnull':
            return self.nonnull(prefix, path, enforce=enforce)
        if relation != 'equal' or type(expected) not in (type(None), bool, int, float, str):
            raise PopulationError('A scalar requirement needs an exact supported literal or nonnull relation')
        try:
            ir._validate_literal(ir.Literal(expected))
        except ir.QueryError as error:
            raise PopulationError(str(error)) from error
        if expected is None or type(expected) is bool:
            return self.truth(prefix, path, expected, enforce=enforce)
        path = tuple(path)
        token = (prefix, path, type(expected), expected, enforce)
        if token in self.visited_values:
            return
        self.visited_values.add(token)
        self._fact(prefix, path, expected, 'equal', enforce)
        expression = self.value(prefix, path)
        self.steps.append({'operator': type(expression).__name__, 'address': list((*prefix, *path)),
                           'requirement': 'value', 'relation': 'equal', 'value': expected,
                           'quantifier': 'input_domain' if enforce else 'matching_witness',
                           'obligation_exported': enforce})
        resolved_path, expression = self._producer(prefix, path, expression)
        if resolved_path != path:
            return self.require_value(prefix, resolved_path, expected, enforce=enforce)
        if isinstance(expression, ir.Literal):
            if type(expression.value) is not type(expected) or expression.value != expected:
                raise PopulationError('A literal contradicts its exact typed scalar requirement')
        elif isinstance(expression, ir.Count):
            if type(expected) is not int or expected < 0:
                raise PopulationError('COUNT requires a nonnegative integer value')
            self._count_interval(prefix, path, expression, ((expected, expected),))
        elif self.path_length(prefix, path, expression) is not None:
            if type(expected) is not int:
                raise PopulationError('A path length requires an integer value')
            self.length_interval(prefix, path, expression, ((expected, expected),))
        elif isinstance(expression, ir.Case):
            options = [(value, 'then' if value is True else 'otherwise') for value in (True, False, None)
                       if value in self.possible(prefix, (*path, 'condition'))
                       and self.can_equal(prefix, (*path, 'then' if value is True else 'otherwise'), expected)]
            value, branch = self.choose(options, 'CASE exact scalar branch')
            self.truth(prefix, (*path, 'condition'), value, enforce=enforce)
            self.require_value(prefix, (*path, branch), expected, enforce=enforce)
        elif isinstance(expression, ir.Call) and expression.name == 'coalesce':
            indices = [i for i in range(len(expression.args)) if self.can_equal(prefix, (*path, 'args', i), expected)
                       and all(self.can_equal(prefix, (*path, 'args', j), None) for j in range(i))]
            index = self.choose(indices, 'COALESCE exact scalar producer')
            for before in range(index):
                self.null(prefix, (*path, 'args', before), enforce=enforce)
            self.require_value(prefix, (*path, 'args', index), expected, enforce=enforce)
        elif not isinstance(expression, ir.Property):
            raise PopulationError('Unsupported exact scalar producer in inward requirements')
        elif enforce:
            self.property_entity(prefix, path, True)

    def nonnull(self, prefix, path, *, enforce=True):
        path = tuple(path)
        token = (prefix, path, 'nonnull', enforce)
        if token in self.visited_values:
            return
        self.visited_values.add(token)
        self._fact(prefix, path, None, 'nonnull', enforce)
        expression = self.value(prefix, path)
        self.steps.append({'operator': type(expression).__name__, 'address': list((*prefix, *path)),
                           'requirement': 'value', 'relation': 'nonnull',
                           'quantifier': 'input_domain' if enforce else 'matching_witness',
                           'obligation_exported': enforce})
        resolved_path, expression = self._producer(prefix, path, expression)
        if resolved_path != path:
            return self.nonnull(prefix, resolved_path, enforce=enforce)
        if isinstance(expression, ir.Literal):
            if expression.value is None:
                raise PopulationError('NULL cannot satisfy a nonnull scalar requirement')
        elif isinstance(expression, ir.Property):
            if expression.key == 'uid' or enforce:
                self.property_entity(prefix, path, True, enforce=enforce)
            return
        elif isinstance(expression, (ir.Count, ir.Exists)):
            return
        elif self.path_length(prefix, path, expression) is not None:
            return
        elif isinstance(expression, ir.Case):
            options = [(value, 'then' if value is True else 'otherwise') for value in (True, False, None)
                       if value in self.possible(prefix, (*path, 'condition'))
                       and not (isinstance(self.value(prefix, (*path, 'then' if value is True else 'otherwise')), ir.Literal)
                                and self.value(prefix, (*path, 'then' if value is True else 'otherwise')).value is None)]
            value, branch = self.choose(options, 'CASE nonnull value branch')
            self.truth(prefix, (*path, 'condition'), value, enforce=enforce)
            self.nonnull(prefix, (*path, branch), enforce=enforce)
        elif isinstance(expression, ir.Call) and expression.name == 'coalesce':
            indices = [i for i in range(len(expression.args))
                       if not (isinstance(expression.args[i], ir.Literal) and expression.args[i].value is None)
                       and all(self.can_equal(prefix, (*path, 'args', j), None) for j in range(i))]
            index = self.choose(indices, 'COALESCE first nonnull producer')
            for before in range(index):
                self.null(prefix, (*path, 'args', before), enforce=enforce)
            self.nonnull(prefix, (*path, 'args', index), enforce=enforce)
        else:
            desired = self.choose([value for value in (False, True) if value in self.possible(prefix, path)],
                                  'non-null Boolean scalar')
            self.truth(prefix, path, desired, enforce=enforce)

    def null(self, prefix, path, *, enforce=True):
        """Require a NULL scalar; unlike Boolean negation, integers stay typed."""
        path = tuple(path)
        expression = self.value(prefix, path)
        producer_path, producer = self._producer(prefix, path, expression)
        if isinstance(producer, ir.Count):
            raise PopulationError('COUNT cannot satisfy a NULL scalar requirement')
        if self.path_length(prefix, producer_path, producer) is not None:
            raise PopulationError('A bound path length cannot satisfy a NULL scalar requirement')
        if isinstance(producer, ir.Literal) and producer.value is not None:
            raise PopulationError('A non-null literal contradicts a NULL scalar requirement')
        self.truth(prefix, path, None, enforce=enforce)

    def _count_interval(self, prefix, path, expression, intervals):
        key = (prefix, tuple(path))
        domain = _intervals_intersection(self.count_domains.get(key, ((0, None),)), intervals)
        if not domain:
            raise PopulationError('COUNT interval intersection is empty')
        self.count_domains[key] = domain
        self.steps.append({'operator': 'Count', 'address': list((*prefix, *path)),
                           'requirement': 'integer_domain', 'intervals': [list(interval) for interval in domain]})
        lower, upper = self.choose(domain, 'COUNT interval-union branch')
        self._subquery_rows(prefix, path, expression, lower, upper)

    def truth(self, prefix, path, desired, *, enforce=True):
        path = tuple(path)
        token = (prefix, path, desired, enforce)
        if token in self.visited_truth:
            return
        self.visited_truth.add(token)
        expression = self.value(prefix, path)
        address = (*prefix, *path)
        if desired is not None and type(desired) is not bool:
            raise PopulationError('A truth requirement must be TRUE, FALSE or NULL')
        if enforce:
            self._fact(prefix, path, desired, 'equal', enforce)
        self.steps.append({'operator': type(expression).__name__, 'address': list(address),
                           'requirement': 'truth', 'value': desired,
                           'quantifier': 'input_domain' if enforce else 'matching_witness',
                           'obligation_exported': enforce,
                           'vacuous_when_no_matching_rows': not enforce})
        resolved_path, resolved = self._producer(prefix, path, expression)
        if resolved_path != path:
            self.truth(prefix, resolved_path, desired, enforce=enforce)
            return
        if isinstance(expression, ir.Literal):
            if expression.value is not desired:
                raise PopulationError('A literal contradicts its inward truth requirement')
        elif isinstance(expression, ir.Negation):
            self.truth(prefix, (*path, 'expression'), None if desired is None else not desired, enforce=enforce)
        elif isinstance(expression, ir.Boolean):
            domains = [self.possible(prefix, (*path, 'terms', i)) for i in range(len(expression.terms))]
            neutral = expression.operator == 'AND'
            if desired is neutral:
                allocations = [tuple(neutral for _ in domains)] if all(neutral in domain for domain in domains) else []
            else:
                if prod(len(domain) for domain in domains) > 4096:
                    raise PopulationError('Three-valued operand allocation exceeds the finite logical budget')
                allocations = [values for values in product(*domains) if _logical(expression.operator, values) is desired]
            allocations.sort(key=lambda values: (sum(value is not neutral for value in values), sum(value is None for value in values)))
            allocation = self.choose(allocations, 'three-valued Boolean operands')
            for index, value in enumerate(allocation):
                self.truth(prefix, (*path, 'terms', index), value, enforce=enforce)
        elif isinstance(expression, ir.Exists):
            if desired is None:
                raise PopulationError('EXISTS cannot satisfy a NULL Boolean requirement')
            self._subquery_rows(prefix, path, expression, 1 if desired else 0, None if desired else 0)
        elif isinstance(expression, ir.All):
            self.all_truth(prefix, path, expression, desired)
        elif isinstance(expression, ir.Comparison):
            left_path, left = self._producer(prefix, (*path, 'left'), expression.left)
            right_path, right = self._producer(prefix, (*path, 'right'), expression.right)
            operator = expression.operator
            if operator in ('IS NULL', 'IS NOT NULL'):
                if desired is None:
                    raise PopulationError('A null test always returns a non-null Boolean')
                if (operator == 'IS NULL') == desired:
                    self.null(prefix, (*path, 'left'), enforce=enforce)
                else:
                    self.nonnull(prefix, (*path, 'left'), enforce=enforce)
                return
            if desired is None:
                candidates = [(left_path, left), (right_path, right)]
                candidates = [(location, part) for location, part in candidates
                              if not isinstance(part, ir.Count) and (not isinstance(part, ir.Literal) or part.value is None)]
                location, _ = self.choose(candidates, 'NULL-producing comparison operand')
                self.null(prefix, location, enforce=enforce)
                return
            if (isinstance(left, ir.Literal) and left.value is None
                    or isinstance(right, ir.Literal) and right.value is None):
                raise PopulationError('A comparison with NULL cannot supply a Boolean value')
            if ((isinstance(right, ir.Count) or self.path_length(prefix, right_path, right) is not None)
                    and isinstance(left, ir.Literal)):
                left_path, right_path, left, right = right_path, left_path, right, left
                operator = {'>': '<', '>=': '<=', '<': '>', '<=': '>='}.get(operator, operator)
            if isinstance(left, ir.Count) and isinstance(right, ir.Literal):
                intervals = self._intervals(operator, right.value, desired)
                if not intervals:
                    raise PopulationError('Unsupported count comparison in an inward truth requirement')
                self._count_interval(prefix, left_path, left, intervals)
            elif self.path_length(prefix, left_path, left) is not None and isinstance(right, ir.Literal):
                intervals = self._intervals(operator, right.value, desired)
                if not intervals:
                    raise PopulationError('Unsupported path length comparison in inward truth requirements')
                self.length_interval(prefix, left_path, left, intervals)
            elif isinstance(right, ir.Literal) or isinstance(left, ir.Literal):
                location, literal = (left_path, right.value) if isinstance(right, ir.Literal) else (right_path, left.value)
                reverse = not isinstance(right, ir.Literal)
                candidates = [literal]
                if type(literal) is bool:
                    candidates.append(not literal)
                elif type(literal) in (int, float):
                    candidates.extend((literal - 1, literal + 1))
                elif type(literal) is str:
                    candidates.extend(('', literal + '_other'))
                choices = []
                for candidate in candidates:
                    try:
                        actual = _compare(literal, operator, candidate) if reverse else _compare(candidate, operator, literal)
                    except (PopulationError, TypeError):
                        continue
                    if actual is desired and self.can_equal(prefix, location, candidate):
                        if not any(type(old) is type(candidate) and old == candidate for old in choices):
                            choices.append(candidate)
                expected = self.choose(choices, 'typed comparison operand representative')
                self.require_value(prefix, location, expected, enforce=enforce)
        elif isinstance(expression, ir.Case):
            possibilities = self.possible(prefix, (*path, 'condition'))
            options = [(value, 'then' if value is True else 'otherwise') for value in (True, False, None) if value in possibilities]
            value, branch = self.choose(options, 'CASE selected value branch')
            self.truth(prefix, (*path, 'condition'), value, enforce=enforce)
            self.truth(prefix, (*path, branch), desired, enforce=enforce)
        elif isinstance(expression, ir.Call) and expression.name == 'coalesce':
            if desired is None:
                for index in range(len(expression.args)):
                    self.null(prefix, (*path, 'args', index), enforce=enforce)
            else:
                indices = [i for i in range(len(expression.args)) if desired in self.possible(prefix, (*path, 'args', i))
                           and all(None in self.possible(prefix, (*path, 'args', j)) for j in range(i))]
                index = self.choose(indices, 'first non-null COALESCE argument')
                for before in range(index):
                    self.null(prefix, (*path, 'args', before), enforce=enforce)
                self.truth(prefix, (*path, 'args', index), desired, enforce=enforce)
        elif not isinstance(expression, (ir.Property, ir.Variable)):
            raise PopulationError('Unsupported scalar producer in inward truth lowering')
        elif isinstance(expression, ir.Property):
            if expression.key == 'uid':
                self.property_entity(prefix, path, desired is not None, enforce=enforce)
            elif desired is not None and enforce:
                self.property_entity(prefix, path, True)

    def domain_factors(self, endpoint, seen=frozenset()):
        if endpoint in seen:
            raise PopulationError('A cyclic population dependency has no finite work coefficient')
        producers = [(key, value) for key, value in self.variables.items() if value['right'] == endpoint]
        if not producers:
            return ()
        # The constructive graph uses fresh target domains. Multiple producers
        # need a sum/max-domain rule rather than a guessed multiplicity.
        if len(producers) != 1:
            raise PopulationError('Ambiguous population producers need an explicit work-domain rule')
        key, value = producers[0]
        return (*self.domain_factors(value['left'], seen | {endpoint}), key) if value['kind'] in ('edge', 'path') else (key,)

    def uniform_scalar(self, prefix, path, seen=frozenset()):
        """Certify invariance over copies of a constructive binding profile.

        The population normal form gives a profile's ordinary scalar
        properties one shared value; physical uid values differ per copy.
        Follow lexical producers rather than trusting the printed alias.
        Graph reads and row-dependent binders have no such certificate.
        """
        path = tuple(path)
        token = (prefix, path)
        if token in seen:
            return False
        expression = self.value(prefix, path)
        if isinstance(expression, ir.Literal):
            return True
        if isinstance(expression, ir.Property):
            return expression.key != 'uid' and self.node_id(path) in self.infos[prefix]['symbols']
        if isinstance(expression, ir.Variable):
            try:
                producer, _ = self._producer(prefix, path, expression)
            except PopulationError:
                return False
            return self.uniform_scalar(prefix, producer, seen | {token})
        if isinstance(expression, (ir.Comparison, ir.Concat)):
            children = ('left',), ('right',)
        elif isinstance(expression, ir.Boolean):
            children = tuple(('terms', index) for index in range(len(expression.terms)))
        elif isinstance(expression, ir.Negation):
            children = (('expression',),)
        elif isinstance(expression, ir.Case):
            children = (('condition',), ('then',), ('otherwise',))
        elif isinstance(expression, ir.ListExpr):
            children = tuple(('items', index) for index in range(len(expression.items)))
        elif isinstance(expression, ir.Call) and expression.name in {'coalesce', 'length', 'range', 'size'}:
            children = tuple(('args', index) for index in range(len(expression.args)))
        else:
            # COUNT/EXISTS/ALL and entity/path producers need graph- or
            # binder-sensitive proofs, not a scalar-profile assumption.
            return False
        return all(self.uniform_scalar(prefix, (*path, *child), seen | {token}) for child in children)

    def known_scalar(self, prefix, path):
        """Evaluate only constants and exported, independently checked facts."""
        path = tuple(path)
        address = (*prefix, *path)
        if address in self.values:
            return True, self.values[address]
        resolved, value = self._producer(prefix, path, self.value(prefix, path))
        if resolved != path:
            return self.known_scalar(prefix, resolved)
        if isinstance(value, ir.Literal):
            return True, value.value
        if isinstance(value, ir.Property):
            sid = self.infos[prefix]['symbols'].get(self.node_id(path))
            identity = (self.canonical(prefix, sid), value.key)
            return (True, self.scalar_values[identity]) if identity in self.scalar_values else (False, None)
        if isinstance(value, ir.Comparison):
            left, a = self.known_scalar(prefix, (*path, 'left'))
            right, b = self.known_scalar(prefix, (*path, 'right'))
            if not left or not right:
                return False, None
            try:
                return True, _compare(a, value.operator, b)
            except (PopulationError, TypeError):
                return False, None
        if isinstance(value, ir.Negation):
            known, result = self.known_scalar(prefix, (*path, 'expression'))
            return (True, None if result is None else not result) if known else (False, None)
        if isinstance(value, ir.Boolean):
            results = [self.known_scalar(prefix, (*path, 'terms', i)) for i in range(len(value.terms))]
            decisive = value.operator == 'OR'
            if any(known and result is decisive for known, result in results):
                return True, decisive
            return ((True, _logical(value.operator, [result for _, result in results]))
                    if all(known for known, _ in results) else (False, None))
        if isinstance(value, ir.Case):
            known, condition = self.known_scalar(prefix, (*path, 'condition'))
            return self.known_scalar(prefix, (*path, 'then' if condition is True else 'otherwise')) if known else (False, None)
        if isinstance(value, ir.Call) and value.name == 'coalesce':
            for index in range(len(value.args)):
                known, result = self.known_scalar(prefix, (*path, 'args', index))
                if not known or result is not None:
                    return known, result
            return True, None
        return False, None

    def atom_factors(self, prefix, query_path, stop=None, *, ignore_filter_at=None):
        """Actual row-prefix factors, including initially unbound node scans."""
        result, bound = [], set()
        query = self.value(prefix, query_path)
        info = self.infos[prefix]
        scope = next((item for item in info['analysis'].scopes
                      if item['node_id'] == self.node_id(query_path) and item['kind'] == 'query'), None)
        if scope:
            bound.update(self.canonical(prefix, sid) for sid in scope['input_bindings'].values())
        for index, (part, location) in enumerate(_clause_addresses(query, tuple(query_path))):
            if stop is not None and index >= stop:
                break
            if isinstance(part, ir.With) and part.distinct:
                raise PopulationError('A repeated-work coefficient cannot cross an unproved WITH DISTINCT')
            factor = self.row_factor(prefix, part, location) if isinstance(part, ir.Match) else None
            if part.where is not None and index != ignore_filter_at and (not isinstance(part, ir.Match)
                    or not part.optional or self.match_state(prefix, part, location)):
                # A witness passing a filter says nothing about how many
                # copies pass it. Multiplication is justified only when the
                # predicate is invariant over the entire constructed domain.
                if not self.uniform_scalar(prefix, (*location, 'where')):
                    operator = 'WITH' if isinstance(part, ir.With) else 'MATCH'
                    raise PopulationError(f'A repeated-work coefficient cannot cross an unproved {operator} input-domain filter')
                self.steps.append({'operator': type(part).__name__, 'address': list((*prefix, *location, 'where')),
                                   'requirement': 'uniform_constructed_domain',
                                   'proof': 'pure_scalar_after_lexical_producer_expansion'})
            if isinstance(part, ir.Match):
                pattern = part.pattern
                if isinstance(pattern, ir.Node):
                    endpoint = self.canonical(prefix, self._sid(prefix, (*location, 'pattern', 'variable')))
                    if endpoint not in bound and factor is not None:
                        result.extend(self.domain_factors(endpoint))
                    bound.add(endpoint)
                else:
                    left = self.canonical(prefix, self._sid(prefix, (*location, 'pattern', 'left', 'variable')))
                    right = self.canonical(prefix, self._sid(prefix, (*location, 'pattern', 'right', 'variable')))
                    if left not in bound and right not in bound:
                        result.extend(self.domain_factors(left))
                        if factor is not None:
                            result.append(factor)
                    elif left in bound and right not in bound:
                        if factor is not None:
                            result.append(factor)
                    # A bound target in the fresh-domain normal form has one
                    # predecessor; matching that predecessor is a filter.
                    bound.update((left, right))
        return tuple(result)

    def query_observer_inputs(self, prefix, query_path, descendant):
        """Certify real row bindings at an observed scalar position.

        RETURN observes the completed pipeline. A WHERE scalar observes its
        matching prefix before applying its own filter. The finite population
        constructor enforces right-local scalar MATCH filters on every valid
        witness; other nonconstant cross-query filters need a separate row
        witness rule and are conservatively refused here.
        """
        query_path, descendant = tuple(query_path), tuple(descendant)
        query = self.value(prefix, query_path)
        parts = _clause_addresses(query, query_path)
        if prefix == ('query',) and not query_path and getattr(self, 'output_minimum', 0) > 0:
            # The caller checks this observable whole-query lower bound on
            # both proof and full graphs. It is stronger than a guessed local
            # filter valuation, and preserves logical/model backtracking.
            self.steps.append({'operator': 'Query', 'address': list(prefix),
                               'requirement': 'scalar_observer_input', 'minimum': 1,
                               'proof': 'checked_whole_query_output_lower_bound'})
            return
        stop, ignored = None, None
        if descendant[len(query_path):len(query_path) + 1] == ('returns',):
            if query.limit == 0:
                raise PopulationError('A scalar projection cannot supply work below LIMIT zero')
            stop = len(parts)
        else:
            for index, (part, location) in enumerate(parts):
                where = (*location, 'where')
                projections = (*location, 'projections')
                if part.where is not None and descendant[:len(where)] == where:
                    stop, ignored = index + 1, index
                    break
                if isinstance(part, ir.With) and descendant[:len(projections)] == projections:
                    stop = index
                    break
        if stop is None:
            return
        for index, (part, location) in enumerate(parts[:stop]):
            if isinstance(part, ir.Match):
                self.row_factor(prefix, part, location)
                if part.optional and not self.match_state(prefix, part, location):
                    continue
            if part.where is None or index == ignored:
                continue
            resolved_path, resolved = self._producer(prefix, (*location, 'where'), part.where)
            literal = isinstance(resolved, ir.Literal)
            refs = set()
            for relative, value in _located(part.where):
                if isinstance(value, ir.Property):
                    sid = self.infos[prefix]['symbols'].get(self.node_id((*location, 'where', *relative)))
                    refs.add(self.canonical(prefix, sid))
                elif isinstance(value, ir.Variable):
                    # The population's positive-local predicate rule does
                    # not certify a WITH alias as a matching filter.
                    refs.add(None)
            right = (self.canonical(prefix, self._sid(prefix, (*location, 'pattern', 'right', 'variable')))
                     if isinstance(part, ir.Match) and isinstance(part.pattern, ir.Edge) else None)
            known, expected = self.known_scalar(prefix, (*location, 'where'))
            if not literal and not (known and expected is True) and not (right is not None and refs == {right}
                                    and self.uniform_scalar(prefix, (*location, 'where'))):
                raise PopulationError('A query-observer input filter has no certified all-row matching witness')
            self.truth(prefix, resolved_path, True, enforce=False)
        factors = self.atom_factors(prefix, query_path, stop=stop, ignore_filter_at=ignored)
        self.bind(factors, 1, address=(*prefix, *query_path))
        self.steps.append({'operator': 'Query', 'address': list((*prefix, *query_path)),
                           'requirement': 'scalar_observer_input', 'minimum': 1,
                           'position': 'projection' if ignored is None and stop == len(parts) else 'pre_filter_prefix'})

    def models(self, fixed=(), *, max_quantity=128, max_models=256):
        keys = tuple(self.variables)
        positions = {key: index for index, key in enumerate(keys)}
        lower = [0] * len(keys)
        fixed = dict(fixed)
        for bound in self.bounds:
            if bound.minimum > 0:
                for key in bound.factors:
                    lower[positions[key]] = 1
        for key, value in fixed.items():
            lower[positions[key]] = value
        def amount(bound, state):
            return prod(state[positions[key]] for key in bound.factors)
        def score(state):
            # Estimate vertex populations from real binding dependencies, not
            # printed names. The constructor remains the final feasibility test.
            domains = {}
            for key, variable in self.variables.items():
                q = state[positions[key]]
                source = domains.get(variable['left'], 1)
                domains[variable['right']] = max(domains.get(variable['right'], 0), source * q)
            return sum(domains.values())
        serial = count()
        queue, seen = [(score(tuple(lower)), next(serial), tuple(lower))], {tuple(lower)}
        emitted, visited = 0, 0
        while queue and emitted < max_models and visited < 50_000:
            _, _, state = heappop(queue)
            visited += 1
            if any(bound.maximum is not None and amount(bound, state) > bound.maximum for bound in self.bounds):
                continue
            violated = next((bound for bound in self.bounds if amount(bound, state) < bound.minimum), None)
            if violated is None:
                emitted += 1
                yield dict(zip(keys, state))
                # A graph model may reject this otherwise valid cardinality
                # allocation. Keep finite upward alternatives available.
                for key, index in positions.items():
                    if key in fixed or state[index] >= max_quantity:
                        continue
                    updated = (*state[:index], state[index] + 1, *state[index + 1:])
                    if updated not in seen:
                        seen.add(updated)
                        heappush(queue, (score(updated), next(serial), updated))
                continue
            for key in dict.fromkeys(violated.factors):
                if key in fixed:
                    continue
                index = positions[key]
                others = prod(state[positions[other]] for other in violated.factors if other != key)
                if not others:
                    continue
                needed = min(max_quantity, ceil(violated.minimum / others))
                for value in range(state[index] + 1, needed + 1):
                    updated = (*state[:index], value, *state[index + 1:])
                    if updated not in seen:
                        seen.add(updated)
                        heappush(queue, (score(updated), next(serial), updated))


def _right_filter_keys(problem, prefix, clause, location):
    """Keys of a real scalar right-endpoint exclusion test, if one exists."""
    if not isinstance(clause.pattern, ir.Edge) or clause.where is None:
        return set()
    target = problem.canonical(prefix, problem._sid(prefix, (*location, 'pattern', 'right', 'variable')))
    terms = list(enumerate(clause.where.terms)) if isinstance(clause.where, ir.Boolean) and clause.where.operator == 'AND' else [(None, clause.where)]
    info = problem.infos[prefix]
    for index, term in terms:
        address = (*location, 'where', *(("terms", index) if index is not None else ()))
        parts = list(_located(term))
        if any(isinstance(part, (ir.Count, ir.Exists, ir.All)) for _, part in parts):
            continue
        refs, keys = set(), set()
        for relative, part in parts:
            if isinstance(part, ir.Property):
                sid = info['symbols'].get(problem.node_id((*address, *relative)))
                refs.add(problem.canonical(prefix, sid))
                keys.add(part.key)
        if refs == {target}:
            return keys
    return set()


def _repeat_axes(problem, site, source, alternative, *, allow_single=False):
    owner = tuple(site.guards.get('query_address', ()))
    indices = site.guards.get('clause_indices')
    if not isinstance(indices, (tuple, list)) or len(indices) != 2:
        raise PopulationError('Repeated work requires an explicitly marked pair of row producers')
    axes, rejected = [], []
    for prefix, tree in ((('query',), source), (('alternatives', 0), alternative)):
        query = node_at(tree, owner)
        parts = _clause_addresses(query, owner)
        for index in indices:
            clause, location = parts[index]
            if not isinstance(clause, ir.Match):
                continue
            if isinstance(clause.pattern, ir.Node) and (clause.where is None
                    or not problem.uniform_scalar(prefix, (*location, 'where'))
                    or not any(isinstance(part, ir.Property) and part.key != 'uid' for _, part in _located(clause.where))):
                continue
            selected = problem.atom(prefix, (*location, 'pattern'))
            if selected is None:
                continue
            try:
                factors = list(problem.atom_factors(prefix, owner, stop=index))
                pattern_path = (*location, 'pattern')
                nodes = (pattern_path,) if isinstance(clause.pattern, ir.Node) else ((*pattern_path, 'left'), (*pattern_path, 'right'))
                inputs = []
                for node_path in nodes:
                    sid = problem._sid(prefix, (*node_path, 'variable'))
                    if problem.infos[prefix]['records'][sid]['definition'] != problem.node_id((*node_path, 'variable')):
                        if problem.nullable_import(prefix, node_path):
                            raise PopulationError('A NULL imported entity cannot supply matching work')
                        inputs.append(node_path)
                if isinstance(clause.pattern, ir.Edge) and (*pattern_path, 'right') in inputs and selected not in factors:
                    raise PopulationError('A pre-bound target has no fresh right-candidate work domain')
            except PopulationError as error:
                rejected.append(str(error))
                continue
            if not factors and not allow_single:
                continue
            producer = None
            kind = 'matching_candidate_fanout'
            if selected in factors and isinstance(clause.pattern, ir.Edge):
                # This endpoint has already been enumerated by the prefix.
                # New endpoints enlarge that producer's input domain. The
                # extra work is the prefix's discrete sensitivity to this
                # population, not a new fanout for every bound endpoint.
                factors.remove(selected)
                kind = 'producer_growth_through_prefix'
                failed_keys = _right_filter_keys(problem, prefix, clause, location)
                if not failed_keys:
                    continue
                right = problem.variables[selected]['right']
                visible = True
                for prior, prior_location in parts[:index]:
                    if not isinstance(prior, ir.Match):
                        continue
                    for relative, node in _located(prior.pattern):
                        if not isinstance(node, ir.Node):
                            continue
                        sid = problem._sid(prefix, (*prior_location, 'pattern', *relative, 'variable'))
                        if problem.canonical(prefix, sid) == right:
                            if any(key in failed_keys for key, _ in node.properties):
                                visible = False
                            if producer is None:
                                producer = (*prefix, *prior_location, 'pattern')
                    if prior.where is not None:
                        for relative, value in _located(prior.where):
                            if isinstance(value, ir.Property) and value.key in failed_keys:
                                sid = problem.infos[prefix]['symbols'].get(problem.node_id((*prior_location, 'where', *relative)))
                                if problem.canonical(prefix, sid) == right:
                                    visible = False
                if not visible or producer is None or not factors:
                    continue
            axis = {'key': selected, 'factors': tuple(factors), 'mode': 'repeat',
                         'axis_kind': kind, 'address': (*prefix, *location, 'pattern'),
                         'producer_read_address': producer,
                         'nonnull_inputs': tuple(inputs),
                         'candidate_kind': 'right_scalar' if isinstance(clause.pattern, ir.Node) or kind == 'producer_growth_through_prefix' else 'right'}
            if factors:
                axes.append(axis)
            if allow_single:
                axes.append({**axis, 'mode': 'candidate', 'axis_kind': 'candidate_work_without_proved_repetition'
                             if kind != 'producer_growth_through_prefix' else kind})
    if not axes:
        detail = f': {rejected[0]}' if rejected else ''
        raise PopulationError('No moved matching atom has a proved repeated-work axis' + detail)
    return axes


def _path_axes(problem, site, source, alternative):
    """Grow candidates behind a mandatory node-local universal guard.

    This axis is selected by a bounded Edge and its actual filter, independently
    of the catalog name of the local rewrite. The graph constructor must prove
    that every added candidate has an explicit FALSE prefix.
    """
    axes = []
    for prefix, tree in ((('query',), source), (('alternatives', 0), alternative)):
        path = tuple(site.address)
        pattern = node_at(tree, path)
        if not isinstance(pattern, ir.Edge) or pattern.relationship.max_hops is None:
            continue
        key = problem.atom(prefix, path)
        parent = problem.value(prefix, path[:-1])
        if getattr(parent, 'optional', False):
            raise PopulationError('An OPTIONAL path needs a separate NULL-extension construction proof')
        predicate = getattr(parent, 'where', None)
        if predicate is None:
            continue

        def guards(value, location):
            if isinstance(value, ir.All):
                if not (isinstance(value.items, ir.Call) and value.items.name == 'nodes'
                        and len(value.items.args) == 1 and isinstance(value.items.args[0], ir.Variable)):
                    return False
                guarded, _ = problem.path_atom(prefix, (*location, 'items', 'args', 0))
                if guarded != key:
                    return False
                problem.all_truth(prefix, location, value, True)
                return True
            return (isinstance(value, ir.Boolean) and value.operator == 'AND'
                    and any(guards(term, (*location, 'terms', index)) for index, term in enumerate(value.terms)))

        if not guards(predicate, (*path[:-1], 'where')):
            continue
        domain = _intervals_intersection(problem.path_domains[key], ((2, 8),))
        if not domain:
            continue
        problem.path_domains[key] = domain
        axes.append({'key': key, 'factors': (), 'mode': 'path',
                     'axis_kind': 'guarded_path_false_prefix', 'address': (*prefix, *path),
                     'producer_read_address': None, 'candidate_kind': 'path_false_prefix'})
    if not axes:
        raise PopulationError('No bounded path has a positive mandatory ALL guard and room for a FALSE-prefix suffix')
    return axes


def _scalar_masks(problem, prefix, path, envelope=(), replacement_port=None):
    """Semantic independence of a scalar read from its enclosing result."""
    masks = []
    for length in range(len(path)):
        parent, child = path[:length], path[length]
        if parent[:len(envelope)] != envelope or len(parent) < len(envelope):
            continue
        if replacement_port is not None and (len(parent) >= len(replacement_port)
                                              or replacement_port[:len(parent)] != parent):
            # A replacement must be expensive when its port is evaluated.
            # Its own dead branches cannot supply work for replacing L by H.
            continue
        value = problem.value(prefix, parent)
        if isinstance(value, (ir.Case, ir.ConditionalQuery)) and child in ('then', 'otherwise'):
            values = (False, None) if child == 'then' else (True,)
            for desired in values:
                if desired in problem.possible(prefix, (*parent, 'condition')):
                    masks.append({'kind': 'unselected_value_branch', 'parent': parent,
                                  'address': (*parent, 'condition'), 'value': desired})
        elif isinstance(value, ir.Boolean) and child == 'terms':
            index = path[length + 1]
            decisive = value.operator == 'OR'
            for other in range(len(value.terms)):
                if other != index and decisive in problem.possible(prefix, (*parent, 'terms', other)):
                    masks.append({'kind': 'absorbing_boolean_sibling', 'parent': parent,
                                  'address': (*parent, 'terms', other), 'value': decisive})
        elif isinstance(value, ir.Call) and value.name == 'coalesce' and child == 'args':
            for other in range(path[length + 1]):
                term = value.args[other]
                if not isinstance(term, ir.Literal) or term.value is not None:
                    masks.append({'kind': 'nonnull_coalesce_prefix', 'parent': parent,
                                  'address': (*parent, 'args', other), 'relation': 'nonnull'})
    return masks


def _activate_scalar_route(problem, prefix, path, start):
    """The hypothetical expensive scalar reaches this read when evaluated.

    An external mask proves result independence. Below that mask, CASE and
    coalesce must actually select the read, and Boolean siblings must be
    neutral. This does not assume a particular optimizer evaluation order.
    """
    for length in range(start, len(path)):
        parent, child = path[:length], path[length]
        value = problem.value(prefix, parent)
        if isinstance(value, ir.Query):
            problem.query_observer_inputs(prefix, parent, path)
        if isinstance(value, ir.RowLimit) and value.limit == 0:
            raise PopulationError('An expensive scalar read cannot be reached below a zero row limit')
        if isinstance(value, (ir.Case, ir.ConditionalQuery)) and child in ('then', 'otherwise'):
            desired = problem.choose([truth for truth in (True, False, None)
                                      if truth in problem.possible(prefix, (*parent, 'condition'))
                                      and ('then' if truth is True else 'otherwise') == child],
                                     'scalar-work active branch')
            problem.truth(prefix, (*parent, 'condition'), desired)
        elif isinstance(value, ir.Boolean) and child == 'terms':
            index = path[length + 1]
            for other in range(len(value.terms)):
                if other != index:
                    problem.truth(prefix, (*parent, 'terms', other), value.operator == 'AND')
        elif isinstance(value, ir.Call) and value.name == 'coalesce' and child == 'args':
            for before in range(path[length + 1]):
                problem.null(prefix, (*parent, 'args', before))


def _scalar_axis(problem, site, seed=0):
    reads = []
    replacement_interface = ('envelope_site' in site.guards
                             and tuple(site.guards['envelope_site']) != tuple(site.address))
    envelope = tuple(site.guards.get('envelope_site', site.address))
    for prefix, tree in problem.trees.items():
        if replacement_interface and prefix != ('alternatives', 0):
            continue
        changed = node_at(tree, site.address)
        for relative, value in _located(changed):
            if isinstance(value, (ir.Count, ir.Exists)):
                path = (*site.address, *relative)
                for mask in _scalar_masks(problem, prefix, path, envelope,
                                          tuple(site.address) if replacement_interface else None):
                    reads.append((prefix, path, value, mask))
    if reads:
        offset = seed % len(reads)
        reads = reads[offset:] + reads[:offset]
    prefix, path, read, mask = problem.choose(reads, 'changed scalar read with a semantic mask')
    if mask.get('relation') == 'nonnull':
        problem.nonnull(prefix, mask['address'])
    else:
        problem.truth(prefix, mask['address'], mask['value'])
    _activate_scalar_route(problem, prefix, path, len(mask['parent']) + 1)
    is_exists = isinstance(read, ir.Exists)
    problem._subquery_rows(prefix, path, read, 0 if is_exists else 1, 0 if is_exists else None)
    keys = []
    for address, key in problem.atoms.items():
        if key is None or address[:len((*prefix, *path))] != (*prefix, *path):
            continue
        local_address = address[len(prefix):]
        owner = next((local_address[:length] for length in reversed(range(len(local_address)))
                      if isinstance(problem.value(prefix, local_address[:length]), (ir.Count, ir.Exists))), None)
        # A Count's row guard can contain another Exists/Count. Its matching
        # atoms belong to that inner read's execution semantics, rather than
        # inheriting Count's full-input work obligation from a structural walk.
        if owner == path:
            keys.append((key, address))
    key, address = problem.choose(keys, 'matching population inside the changed masked scalar')
    local = address[len(prefix):]
    pattern = problem.value(prefix, local)
    node_paths = (local,) if isinstance(pattern, ir.Node) else ((*local, 'left'), (*local, 'right'))
    for node_path in node_paths:
        sid = problem._sid(prefix, (*node_path, 'variable'))
        if problem.infos[prefix]['records'][sid]['definition'] != problem.node_id((*node_path, 'variable')):
            problem.entity(prefix, node_path, True)
            if isinstance(pattern, ir.Edge) and node_path == (*local, 'right'):
                raise PopulationError('A masked matching population needs a fresh target work domain')
    input_factors = ()
    # A failed existence population in a later MATCH is work only if at least
    # one row reaches it. Use the same certified transfer as repeated-work
    # prefixes, rather than assuming that earlier exact-zero atoms run it.
    for length in reversed(range(len(local))):
        owner_path = local[:length]
        owner = problem.value(prefix, owner_path)
        if not isinstance(owner, ir.Query):
            continue
        clauses = _clause_addresses(owner, owner_path)
        selected = next(((index, clause, location) for index, (clause, location) in enumerate(clauses)
                         if isinstance(clause, ir.Match) and (*location, 'pattern') == local), None)
        if selected is None:
            continue
        index, clause, location = selected
        input_factors = problem.atom_factors(prefix, owner_path, stop=index)
        if not is_exists and clause.where is not None and not problem.uniform_scalar(prefix, (*location, 'where')):
            raise PopulationError('A scalar count work population needs a uniform matching-domain filter')
        problem.bind(input_factors, 1, address=address)
        break
    else:
        parent = problem.value(prefix, local[:-1])
        if not is_exists and getattr(parent, 'where', None) is not None and not problem.uniform_scalar(prefix, (*local[:-1], 'where')):
            raise PopulationError('A scalar count work population needs a uniform matching-domain filter')
    if is_exists and problem.variables[key]['kind'] != 'edge':
        node_path = address[len(prefix):]
        parent = problem.value(prefix, node_path[:-1])
        if getattr(parent, 'where', None) is None:
            raise PopulationError('Masked node existence work needs an actual scalar candidate filter')
    return {'key': key, 'factors': (), 'mode': 'masked_candidates' if is_exists else 'masked',
            'axis_kind': 'masked_scalar_existence_candidates' if is_exists else 'masked_scalar_count_population',
            'address': address, 'producer_read_address': (*prefix, *path),
            'input_factors': input_factors,
            'candidate_kind': 'right_scalar' if is_exists and problem.variables[key]['kind'] == 'node' else 'right' if is_exists else None,
            'semantic_mask': {'kind': mask['kind'], 'address': list((*prefix, *mask['parent'])),
                              'guard_address': list((*prefix, *mask['address']))}}


def _lower_candidate(source, alternative, site, goals, minimum, mode, scale, correspondence, decisions, seed):
    problem = _Lowering(source, alternative, correspondence, decisions)
    problem.output_minimum = minimum
    problem.active_path = tuple(site.guards.get('envelope_site', site.guards.get('query_address', site.address)))
    for goal in goals:
        if goal['kind'] not in ('value', 'binding'):
            continue
        address = tuple(goal.get('address', ()))
        prefix = ('query',) if address[:1] == ('query',) else ('alternatives', 0)
        if address[:len(prefix)] != prefix or address[len(prefix):] not in problem.infos[prefix]['lookup']:
            raise PopulationError('A scalar value goal must address an actual source/alternative expression')
        if goal['kind'] == 'binding':
            relation = goal.get('relation')
            if relation not in ('null', 'nonnull') or not isinstance(problem.value(prefix, address[len(prefix):]), ir.Node):
                raise PopulationError('A binding goal needs an actual Node and null/nonnull relation')
            problem.entity(prefix, address[len(prefix):], relation == 'nonnull')
        else:
            problem.require_value(prefix, address[len(prefix):], goal.get('value'), relation=goal.get('relation', 'equal'))
    for premise in site.guards.get('premises', ()):
        relation = premise['relation']
        if relation == 'nonnull':
            problem.nonnull(('query',), tuple(premise['address']))
        elif relation == 'equal':
            problem.require_value(('query',), tuple(premise['address']), premise.get('value'))
        elif relation in ('true', 'false', 'null'):
            problem.truth(('query',), tuple(premise['address']), {'true': True, 'false': False, 'null': None}[relation])
        else:
            raise PopulationError('Unsupported semantic mutation premise')
    problem.rows(('query',), (), minimum)
    problem.activate_mark()
    target = node_at(alternative, site.address)
    bounded_target = isinstance(target, ir.Edge) and target.relationship.max_hops is not None
    effective = ('path' if bounded_target else 'repeat' if 'clause_indices' in site.guards else 'masked' if isinstance(target, ir.RowStream)
                 else 'scalar') if mode == 'auto' else mode
    if effective == 'path':
        axes = _path_axes(problem, site, source, alternative)
    elif effective == 'repeat':
        axes = _repeat_axes(problem, site, source, alternative, allow_single=mode == 'auto')
    elif effective == 'masked':
        if not isinstance(target, ir.RowStream):
            return problem, _scalar_axis(problem, site, seed)
        before = set(problem.variables)
        problem.rows(('alternatives', 0), site.address, 1)
        axes = [{'key': key, 'factors': (), 'mode': 'masked', 'axis_kind': 'masked_matching_population',
                 'address': variable['addresses'][0], 'producer_read_address': None}
                for key, variable in problem.variables.items() if key not in before]
        if not axes:
            raise PopulationError('The replacement has no matching work axis to amplify')
    elif effective == 'scalar':
        return problem, _scalar_axis(problem, site, seed)
    elif effective is None:
        axes = [{'key': None, 'factors': (), 'mode': None, 'axis_kind': None,
                 'address': (), 'producer_read_address': None}]
    else:
        raise PopulationError('Unknown work amplification mode')
    primary = [axis for axis in axes if axis['mode'] != 'candidate']
    fallback = [axis for axis in axes if axis['mode'] == 'candidate']
    def rotated(group):
        offset = seed % len(group) if group else 0
        return group[offset:] + group[:offset]
    axes = [*rotated(primary), *rotated(fallback)]
    axis = problem.choose(axes, 'work axis from changed matching occurrences')
    prefix = ('query',) if axis['address'][:1] == ('query',) else ('alternatives', 0)
    for node_path in axis.get('nonnull_inputs', ()):
        problem.entity(prefix, node_path, True)
    if axis['mode'] in ('repeat', 'candidate'):
        problem.bind(axis['factors'], 2 if axis['mode'] == 'repeat' else 1, address=axis['address'])
    for key, domain in problem.path_domains.items():
        lengths = tuple(length for lower, upper in domain for length in range(lower, upper + 1))
        length = problem.choose(lengths, 'bounded path length witness')
        problem.path_domains[key] = ((length, length),)
    return problem, axis


def derive_requirement_plans(source, alternative, site, *, goals, scale, correspondence=()):
    """Enumerate bounded logical, work-axis and integer allocation candidates.

    Boolean facts are exact TRUE/FALSE/NULL values. Cardinality constraints
    intersect count interval unions. A rejected graph model can request the
    next candidate without fixing an arbitrary operand or matching axis.
    """
    if type(scale) is not int or scale < 1:
        raise PopulationError('A semantic work budget must be a positive integer')
    if not isinstance(goals, (tuple, list)) or any(not isinstance(goal, dict) for goal in goals):
        raise PopulationError('Semantic goals must be a sequence of goal records')
    if any(goal.get('kind') not in {'output', 'work', 'value', 'binding'} for goal in goals):
        raise PopulationError('Unknown semantic construction goal')
    outputs = [goal.get('min_rows', 1) for goal in goals if goal.get('kind') == 'output']
    if any(type(value) is not int or value < 0 for value in outputs):
        raise PopulationError('An output goal requires a nonnegative row minimum')
    minimum = max(outputs or [0])
    works = [goal for goal in goals if goal.get('kind') == 'work']
    if len(works) > 1:
        raise PopulationError('This finite allocator accepts one marked work objective')
    mode = works[0].get('mode') if works else None
    seed = works[0].get('seed', 0) if works else 0
    if mode not in (None, 'repeat', 'masked', 'auto') or type(seed) is not int or seed < 0:
        raise PopulationError('Unknown work mode or invalid strategy seed')
    pending, candidates, visited, last_error = [()], [], 0, None
    while pending and visited < _BRANCH_BUDGET:
        decisions = pending.pop()
        visited += 1
        try:
            problem, axis = _lower_candidate(source, alternative, site, goals, minimum, mode, scale,
                                             correspondence, decisions, seed)
            fixed = ((axis['key'], scale if axis['mode'] == 'masked' else 0),) if axis['mode'] in ('masked', 'masked_candidates') else ()
            candidates.append((problem, axis, iter(problem.models(fixed, max_models=_ALLOCATIONS_PER_BRANCH))))
        except _ChoiceRequired as choice:
            pending.extend((*decisions, index) for index in reversed(range(choice.width)))
        except PopulationError as error:
            last_error = error
    candidates.sort(key=lambda candidate: candidate[1]['mode'] == 'candidate')
    emitted = 0
    # Interleave work/logical branches so rejection of the first graph does not
    # exhaust a long quantity enumeration before trying another moved atom.
    while candidates and emitted < _PLAN_BUDGET:
        survivors = []
        for problem, axis, models in candidates:
            try:
                quantities = next(models)
            except StopIteration:
                continue
            growth, effective = axis['key'], axis['mode']
            repetitions = prod(quantities[key] for key in axis['factors'])
            extra_per_input = ceil(scale / repetitions) if effective in ('repeat', 'candidate', 'path') else 0
            candidate_kind = axis.get('candidate_kind')
            demands = []
            for index, (key, variable) in enumerate(problem.variables.items()):
                growing = key == growth and effective == 'masked'
                candidates_per_input = (quantities[key] + extra_per_input if effective in ('repeat', 'candidate', 'path') else scale) if key == growth and effective in ('repeat', 'candidate', 'path', 'masked_candidates') else None
                for address in variable['addresses']:
                    demands.append(MatchDemand(address, f'port_{index}', exact_matches=None if growing else quantities[key],
                                               candidate_matches=candidates_per_input, scale_matches=growing,
                                               candidate_kind=candidate_kind if key == growth else None))
                    if variable['kind'] == 'path':
                        lower, upper = problem.path_domains[key][0]
                        prefix = ('query',) if address[:1] == ('query',) else ('alternatives', 0)
                        truths = tuple((fact, desired) for fact, desired in problem.path_truths[key].items()
                                       if fact[:len(prefix)] == prefix)
                        demands.append(PathDemand(address, min_length=lower, max_length=upper, all_values=truths))
            obligations = (*tuple(ValueDemand(address, value) for address, value in problem.values.items()),
                           *tuple(ValueDemand(address, relation='nonnull') for address in problem.nonnulls),
                           *tuple(EntityBindingDemand(address, relation) for address, relation in problem.entities.items()))
            evidence = {
                'goals': list(goals), 'output_min_rows': minimum, 'inward_steps': problem.steps,
                'cardinality_bounds': [{'address': list(bound.address), 'minimum': bound.minimum, 'maximum': bound.maximum,
                                        'factors': [list(problem.variables[key]['addresses'][0]) for key in bound.factors]}
                                       for bound in problem.bounds],
                'count_domains': [{'address': list((*prefix, *path)), 'intervals': [list(interval) for interval in domain]}
                                  for (prefix, path), domain in problem.count_domains.items()],
                'path_domains': [{'address': list(problem.variables[key]['addresses'][0]),
                                  'intervals': [list(interval) for interval in domain],
                                  'all_values': [{'address': list(address), 'value': desired}
                                                 for address, desired in problem.path_truths[key].items()]}
                                 for key, domain in problem.path_domains.items()],
                'allocation': [{'address': list(variable['addresses'][0]), 'per_input_matches': quantities[key],
                                'growth': 'valid' if key == growth and effective == 'masked' else 'candidates' if key == growth else 'none'}
                               for key, variable in problem.variables.items()],
                'work': {'mode': effective, 'requested_mode': mode, 'target': scale, 'repetitions': repetitions,
                         'axis_kind': axis['axis_kind'], 'address': list(axis['address']),
                         'side': axis['address'][0] if axis['address'] else None,
                         'candidate_kind': candidate_kind,
                         'nontrivial_repetition': repetitions >= 2,
                         'coefficient_semantics': 'lower_bound_on_extra_prefix_rows_per_added_match'
                         if axis['axis_kind'] == 'producer_growth_through_prefix' else 'candidate_reads_per_input_matching_population',
                         'producer_read_address': list(axis['producer_read_address']) if axis['producer_read_address'] else None,
                         'semantic_mask': axis.get('semantic_mask'),
                         'input_rows_lower_bound': prod(quantities[key] for key in axis.get('input_factors', ())),
                         'extra_candidates_per_input': extra_per_input,
                         'potential_extra_candidate_work': repetitions * extra_per_input if effective in ('repeat', 'candidate', 'path') else scale if effective in ('masked', 'masked_candidates') else 0},
                'copy_alignment': 'explicit_node_origin_correspondence',
                'search': 'bounded_logical_branches_and_dependency_ordered_integer_allocations',
                'search_budget': {'logical_nodes_visited': visited, 'logical_branches': len(candidates),
                                  'max_logical_nodes': _BRANCH_BUDGET, 'max_plans': _PLAN_BUDGET,
                                  'max_allocations_per_branch': _ALLOCATIONS_PER_BRANCH, 'max_quantity': 128,
                                  'pending_logical_branches': bool(pending), 'complete': False},
                'decisions': problem.decisions,
            }
            emitted += 1
            yield RequirementPlan(tuple(demands), obligations, evidence)
            survivors.append((problem, axis, models))
            if emitted == _PLAN_BUDGET:
                break
        candidates = survivors
    if not emitted:
        raise PopulationError('No bounded cardinality allocation satisfies the inward semantic and work goals'
                              + (f': {last_error}' if last_error else ''))
