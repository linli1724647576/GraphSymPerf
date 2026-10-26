"""M2: insert one local rule into an already sampled typed host."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
import random

import ir
from dependencies import find_mutation_sites, node_at, replace_at
from symbolic import (Hole, MutationMark, Symbol, _assignment, _resolve)

from .contracts import (EmbeddedAst, HostAst, PatternInstance, Port, StageFailure,
                        TerminalSlot, digest)
from .pattern_registry import DEFAULT_REGISTRY, PatternSpec


def _walk(value, address=()):
    yield address, value
    if is_dataclass(value):
        for field in fields(value):
            yield from _walk(getattr(value, field.name), (*address, field.name))
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            yield from _walk(item, (*address, index))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, (*address, key))


def _fix_structure(value, rng, choices):
    """Resolve grammar alternatives in M2; graph-related terminals survive."""
    if isinstance(value, Hole):
        if value.name not in choices:
            choices[value.name] = rng.randrange(len(value.candidates))
        return _fix_structure(value.candidates[choices[value.name]], rng, choices)
    if isinstance(value, (Symbol, TerminalSlot)):
        return value
    if is_dataclass(value):
        return replace(value, **{f.name: _fix_structure(getattr(value, f.name), rng, choices)
                                 for f in fields(value)})
    if isinstance(value, tuple):
        return tuple(_fix_structure(item, rng, choices) for item in value)
    if isinstance(value, list):
        return [_fix_structure(item, rng, choices) for item in value]
    if isinstance(value, dict):
        return {key: _fix_structure(item, rng, choices) for key, item in value.items()}
    return value


def _names(value):
    names = set()
    for _, item in _walk(value):
        if isinstance(item, (ir.Node, ir.Relationship, ir.All)) and item.variable:
            names.add(item.variable)
        if isinstance(item, ir.Property):
            names.add(item.variable)
        if isinstance(item, (ir.Variable, ir.Alias)):
            names.add(item.name)
        if isinstance(item, ir.Edge) and item.path_variable:
            names.add(item.path_variable)
    return names


def _rename_local(value, names, prefix):
    if isinstance(value, Symbol):
        return replace(value, name=prefix + value.name)
    if isinstance(value, TerminalSlot):
        return replace(value, slot_id=prefix + value.slot_id)
    if is_dataclass(value):
        changed = {f.name: _rename_local(getattr(value, f.name), names, prefix) for f in fields(value)}
        if isinstance(value, (ir.Node, ir.Relationship, ir.All)) and value.variable:
            changed['variable'] = names[value.variable]
        if isinstance(value, ir.Property):
            changed['variable'] = names[value.variable]
        if isinstance(value, (ir.Variable, ir.Alias)):
            changed['name'] = names[value.name]
        if isinstance(value, ir.Edge) and value.path_variable:
            changed['path_variable'] = names[value.path_variable]
        return replace(value, **changed)
    if isinstance(value, tuple):
        return tuple(_rename_local(item, names, prefix) for item in value)
    if isinstance(value, list):
        return [_rename_local(item, names, prefix) for item in value]
    if isinstance(value, dict):
        return {key: _rename_local(item, names, prefix) for key, item in value.items()}
    return value


def _symbolic_terminals(value):
    """Adapter used only to validate types, never to construct a graph."""
    if isinstance(value, TerminalSlot):
        kind = ('identifier' if value.kind in {'identifier', 'label', 'relationship_type', 'property_key'}
                else 'integer' if value.kind == 'integer' else 'scalar')
        return Symbol(value.slot_id, kind, value.candidates)
    if is_dataclass(value):
        return replace(value, **{f.name: _symbolic_terminals(getattr(value, f.name)) for f in fields(value)})
    if isinstance(value, tuple):
        return tuple(_symbolic_terminals(item) for item in value)
    if isinstance(value, list):
        return [_symbolic_terminals(item) for item in value]
    if isinstance(value, dict):
        return {key: _symbolic_terminals(item) for key, item in value.items()}
    return value


def _lift(rows, port, placeholder):
    """Declared row-observation adapters preserve the existing host interface."""
    if port.kind == 'rows':
        return rows, (), 'identity'
    expected = getattr(placeholder, 'expected_type', None)
    if port.kind == 'predicate' or expected in ('boolean', 'bool'):
        return ir.Exists(body=rows), ('body',), 'exists'
    if port.kind == 'scalar' and expected in ('integer', 'int', 'number', 'any'):
        return ir.Count(body=rows), ('body',), 'count'
    raise StageFailure('M2', 'no_compatible_site',
                       f'No declared rows observation adapter for {port.kind}/{expected}')


def _native_options(spec, port, placeholder, options):
    """A scalar law must preserve the actual port value, not only row count."""
    if not spec.native_scalar or port.kind == 'rows':
        return dict(options), False
    expected = 'boolean' if port.kind == 'predicate' else placeholder.expected_type
    expected = {'bool': 'boolean', 'int': 'integer'}.get(expected, expected)
    if expected not in {'boolean', 'integer', 'float', 'string'}:
        raise StageFailure('M2', 'no_compatible_site', 'A native scalar port needs a concrete scalar type')
    if spec.pattern_id in {'P4', 'P5', 'S1', 'S2', 'S8'} and expected != 'boolean':
        raise StageFailure('M2', 'no_compatible_site', 'A Boolean Pattern cannot produce an integer by counting its rows')
    result = dict(options)
    if spec.pattern_id == 'S8':
        return result, True
    if result.get('scalar_type', expected) != expected:
        raise StageFailure('M2', 'no_compatible_site', 'Pattern scalar_type differs from the insertion port')
    result['scalar_type'] = expected
    return result, True


def _native_scalar(local, spec, site_address):
    """Expose a local scalar law without discarding its projected value.

    The initial native production has no imports. Free anchor properties
    are declared scalar terminals; graph-reading subqueries retain their own
    MATCH definitions. No MATCH is appended outside the insertion port.
    """
    prefix = ('returns', 1, 'expression')
    if (not isinstance(local.source, ir.Query) or len(local.source.returns) != 2
            or not isinstance(local.source.returns[1], ir.Alias)
            or tuple(site_address)[:len(prefix)] != prefix):
        raise StageFailure('M2', 'interface_mismatch', 'A scalar rule must expose its declared scalar result')
    expression = local.source.returns[1].expression
    expected = True if spec.pattern_id == 'S8' else local.options['scalar_value']
    contexts = tuple(local.options.get('scalar_contexts', ()))

    def free_value(prop):
        key = prop.key.candidates[0] if isinstance(prop.key, Symbol) else prop.key
        if key == 'payload':
            value = expected
        elif key == 'prefix' or key.startswith('null_prefix_'):
            value = None
        elif key == 'decisive':
            value = spec.pattern_id not in {'P4', 'S2'}
        elif key.startswith('neutral_'):
            index = int(key.rsplit('_', 1)[1])
            value = contexts[index] == 'and'
        elif key.startswith('guard_'):
            index = int(key.rsplit('_', 1)[1])
            value = contexts[index] == 'case_then'
        else:
            raise StageFailure('M2', 'interface_mismatch', f'No native terminal role for {key}')
        name = prop.key.name if isinstance(prop.key, Symbol) else f'{prop.variable}:{key}'
        return ir.Literal(Symbol('native:' + name, 'scalar', (value,)))

    def visit(value, bound=frozenset()):
        if isinstance(value, ir.Property) and value.variable not in bound:
            if value.variable != local.anchor.variable:
                raise StageFailure('M2', 'binding_capture', 'A native rule contains an undeclared free variable')
            return free_value(value)
        if isinstance(value, (Symbol, TerminalSlot)):
            return value
        if isinstance(value, (ir.Count, ir.Exists)):
            if value.body is not None:
                return replace(value, body=visit(value.body, bound))
            # The short EXISTS form requires an imported binding in the IR.
            # A full local query instead defines the same graph-reading input.
            body = ir.Query(value.pattern, value.where, (ir.Alias(ir.Literal(1), 'native_witness'),))
            return type(value)(body=visit(body, bound))
        if isinstance(value, ir.Query):
            environment, clauses = set(bound), []
            for clause in ir.clauses(value):
                if isinstance(clause, ir.Match):
                    environment.update(_names(clause.pattern))
                    clauses.append(replace(clause, pattern=visit(clause.pattern, frozenset(environment)),
                                           where=visit(clause.where, frozenset(environment))))
                else:
                    projections = visit(clause.projections, frozenset(environment))
                    environment = {item.name for item in clause.projections}
                    clauses.append(replace(clause, projections=projections,
                                           where=visit(clause.where, frozenset(environment))))
            returns = visit(value.returns, frozenset(environment))
            return replace(ir.with_clauses(value, tuple(clauses)), returns=returns)
        if isinstance(value, ir.All):
            return replace(value, items=visit(value.items, bound),
                           predicate=visit(value.predicate, bound | {value.variable}))
        if is_dataclass(value):
            return replace(value, **{f.name: visit(getattr(value, f.name), bound) for f in fields(value)})
        if isinstance(value, tuple):
            return tuple(visit(child, bound) for child in value)
        if isinstance(value, list):
            return [visit(child, bound) for child in value]
        if isinstance(value, dict):
            return {key: visit(child, bound) for key, child in value.items()}
        return value

    native = visit(expression)
    replacement = visit(local.replacement)
    intents = local.intents
    if spec.pattern_id == 'S8':
        # These are local witness/quantity obligations. The shared relational
        # solver combines them with every host read, rather than using the
        # historical common-anchor-only population adapter.
        witness, low = native.body.branches
        intents = (
            {'kind': 'quantity', 'subject': witness.pattern, 'role': 'existential_witness',
             'valid': 1, 'growth': 'none'},
            {'kind': 'quantity', 'subject': low.pattern, 'role': 'low_candidates',
             'valid': 1, 'growth': 'none'},
            {'kind': 'quantity', 'subject': replacement.pattern, 'role': 'high_candidates',
             'valid': 1, 'growth': 'valid'},
        )
    goals = []
    for goal in local.goals:
        if 'address' not in goal:
            goals.append(goal)
            continue
        address = tuple(goal['address'])
        if address[:len(prefix) + 1] != ('query', *prefix):
            raise StageFailure('M2', 'interface_mismatch', 'A scalar goal escapes its declared native expression')
        goals.append({**goal, 'address': ('query', *address[len(prefix) + 1:])})
    relative = tuple(site_address)[len(prefix):]
    return replace(local, source=native, replacement=replacement, intents=intents, goals=tuple(goals),
                   mark=MutationMark('native:scalar', relative)), relative


def _select_specs(registry, selection_policy, rng):
    if selection_policy is None:
        patterns, form, options, requested_port = list(registry), None, {}, None
    elif isinstance(selection_policy, str):
        patterns, form, options, requested_port = [selection_policy], None, {}, None
    elif isinstance(selection_policy, dict):
        target = selection_policy.get('pattern', selection_policy.get('pattern_id'))
        patterns = [target] if target else list(selection_policy.get('patterns', registry))
        form = selection_policy.get('form')
        options = dict(selection_policy.get('options', {}))
        requested_port = selection_policy.get('port_id')
    else:
        raise StageFailure('M2', 'invalid_selection_policy', 'Expected a Pattern name or selection mapping')
    if not patterns or any(not isinstance(pattern, str) for pattern in patterns):
        raise StageFailure('M2', 'invalid_selection_policy', 'A nonempty Pattern selection is required')
    rng.shuffle(patterns)
    try:
        specs = [registry.get(pattern, form) for pattern in patterns]
    except (ValueError, KeyError) as error:
        raise StageFailure('M2', 'unsupported_pattern', str(error)) from error
    if any(not isinstance(spec, PatternSpec) for spec in specs):
        raise StageFailure('M2', 'invalid_registry', 'Registry entries must be PatternSpec records')
    return specs, options, requested_port


def embed_patterns(host, registry=None, selection_policy=None, embed_seed=0, budget=None):
    """Insert one rule, preserving the complete outside-port host exactly.

    Row rules expose explicit COUNT/EXISTS observation adapters for scalar
    and predicate ports. The adapters are local declared structure in M2;
    they do not rebuild, wrap or select the outer query.
    """
    if not isinstance(host, HostAst):
        raise StageFailure('M2', 'invalid_host', 'Embedding requires an M1 HostAst record')
    if type(embed_seed) is not int or embed_seed < 0:
        raise StageFailure('M2', 'invalid_seed', 'The embedding seed must be nonnegative')
    if not host.ports:
        raise StageFailure('M2', 'no_compatible_site', 'The host declares no insertion port')
    if len(host.ports) != 1:
        raise StageFailure('M2', 'unsupported_fragment',
                           'This embedding version accepts one declared Pattern port')
    remaining = [value for _, value in _walk(host.tree) if isinstance(value, Port)]
    if len(remaining) != 1:
        raise StageFailure('M2', 'unsupported_fragment',
                           'The current embedding contract accepts one unfilled Pattern port')
    registry = registry or DEFAULT_REGISTRY
    rng = random.Random(embed_seed)
    specs, options, requested_port = _select_specs(registry, selection_policy, rng)
    ports = [port for port in host.ports if requested_port is None or port.port_id == requested_port]
    rng.shuffle(ports)
    if not ports:
        raise StageFailure('M2', 'no_compatible_site', 'The requested port does not exist')
    failure = None
    for port in ports:
        try:
            placeholder = node_at(host.tree, port.address)
        except (ValueError, TypeError, AttributeError, IndexError, KeyError) as error:
            raise StageFailure('M2', 'contract_drift', 'The recorded host port is no longer present') from error
        if not isinstance(placeholder, Port) or placeholder.port_id != port.port_id:
            raise StageFailure('M2', 'contract_drift', 'A recorded port must identify its actual placeholder')
        for spec in specs:
            try:
                local_options, native = _native_options(spec, port, placeholder, options)
                local = spec.build_local(local_options, embed_seed)
                choices = {}
                local = _fix_structure(local, rng, choices)
                used, names = _names(host.tree), _names((local.source, local.replacement, local.anchor, local.intents))
                prefix, suffix = 'pattern_0__', 0
                while any(prefix + name in used for name in names):
                    suffix += 1
                    prefix = f'pattern_{suffix}__'
                mapping = {name: prefix + name for name in names}
                local = _rename_local(local, mapping, prefix)
                probe = _symbolic_terminals(local)
                candidate = _resolve(probe.source, _assignment(probe))
                sites = find_mutation_sites(candidate, spec.pattern_id, spec.form)
                if local.mark is not None:
                    sites = [site for site in sites if site.address == local.mark.address]
                if len(sites) != 1:
                    raise StageFailure('M2', 'interface_mismatch', 'A local rule needs one explicit applicable site')
                site = sites[0]
                relative_site = site.address
                if native:
                    local, relative_site = _native_scalar(local, spec, site.address)
                    inserted, lift_prefix, adapter = local.source, (), 'native_scalar'
                else:
                    inserted, lift_prefix, adapter = _lift(local.source, port, placeholder)
                semantic_nodes = sum(is_dataclass(item) and not isinstance(item, (Symbol, TerminalSlot))
                                     for _, item in _walk(inserted))
                reserved = port.reserved_budget
                if isinstance(budget, dict):
                    reserved = min(reserved, budget.get('max_local_nodes', reserved))
                if semantic_nodes > reserved:
                    raise StageFailure('M2', 'embedding_budget_exhausted',
                                       f'Local rule uses {semantic_nodes} AST nodes; port reserves {reserved}')
                tree = replace_at(host.tree, port.address, inserted)
                site_address = (*port.address, *lift_prefix, *relative_site)
                goals = tuple({**goal, 'address': ('query', *port.address, *lift_prefix, *tuple(goal['address'])[1:])}
                              if 'address' in goal and tuple(goal['address'])[:1] == ('query',) else goal
                              for goal in local.goals)
                skeleton = replace(local, source=tree,
                                   mark=MutationMark('pattern:0', site_address), goals=goals)
                resolved_probe = _symbolic_terminals(skeleton)
                ir.validate_query(_resolve(resolved_probe.source, _assignment(resolved_probe)))
                restored = replace_at(tree, port.address, placeholder)
                if digest(restored) != digest(host.tree):
                    raise StageFailure('M2', 'out_of_region_insertion', 'The host outside the port changed')
                instance = PatternInstance('pattern:0', spec.pattern_id, spec.form, port.port_id,
                                           site_address=site_address, imports=(), exports=(),
                                           rule_version=spec.version)
                origin = {'host_digest': host.host_digest, 'port_address': list(port.address),
                          'local_prefix': prefix, 'symbol_map': mapping, 'adapter': adapter,
                          'pattern_spec': spec.to_data(), 'structure_choices': choices,
                          'local_origins': {str(list((*port.address, *lift_prefix, *path))): list(path)
                                            for path, item in _walk(local.source) if is_dataclass(item)}}
                return EmbeddedAst(host, tree, skeleton, (instance,), instance.instance_id,
                                   origin_map=origin, outside_port_digest=digest(host.tree))
            except StageFailure as error:
                failure = error
            except (ValueError, TypeError) as error:
                failure = StageFailure('M2', 'interface_mismatch', str(error))
    raise failure or StageFailure('M2', 'no_compatible_site', 'No rule is compatible with any recorded port')
