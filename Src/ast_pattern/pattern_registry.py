"""Local query laws and declarative interfaces; no host or graph recipes."""

from __future__ import annotations

from dataclasses import dataclass

from paper_patterns import COMPOSITION_PATTERNS, PAPER_CATALOG, compositional_rule


_PREMISES = {
    'P1': ('undirected_edge_binding_bijection',),
    'P2': ('inverse_relationship_bijection', 'unobserved_relationship_identity'),
    'P3': ('bounded_undirected_path', 'reversal_invariant_observations'),
    'P4': ('pure_total_three_valued_boolean_operands',),
    'P5': ('pure_total_three_valued_boolean_operands',),
    'P6': ('same_typed_null_aware_equality',),
    'P7': ('same_conjunctive_label_set',),
    'P8': ('same_relationship_type_alternatives',),
    'P9': ('same_lexical_inputs_at_both_mandatory_matches',),
    'P10': ('per_binding_mandatory_join_bijection',),
    'P11': ('independent_optional_branches', 'per_input_null_extension'),
    'P12': ('same_output_schema', 'bag_union_preserves_multiplicity'),
    'P13': ('null_safe_guard_complement', 'compatible_branch_types'),
    'S1': ('decisive_true_disjunct_per_reachable_input',),
    'S2': ('decisive_false_conjunct_per_reachable_input',),
    'S3': ('checked_selected_query_branch', 'compatible_output_schemas'),
    'S4': ('checked_selected_scalar_branch', 'compatible_branch_types'),
    'S5': ('zero_output_demand',),
    'S6': ('per_input_sufficient_witness_rows', 'same_constant_projection', 'streamable_branches'),
    'S7': ('checked_nonnull_prefix_per_reachable_input',),
    'S8': ('per_input_preserved_existential_witness', 'streamable_branches'),
    'S9': ('empty_mandatory_input', 'independent_downstream_match'),
    'S10': ('false_decisive_guard', 'null_safe_membership', 'preserved_distinct_envelope'),
}

_STATE_NEEDS = {
    'P2': ('inverse_pair',),
    'P3': ('path_observation', 'valid_path_witness', 'failed_candidate_growth'),
    'P11': ('per_binding_optional_quantity', 'null_extension'),
    'S6': ('per_binding_witness_quantity', 'constant_row_projection'),
    'S8': ('per_binding_existential_witness',),
}


@dataclass(frozen=True)
class PatternSpec:
    """One local law, independent of the randomly sampled outer query.

    Graph obligations are lowered from the local AST and these declared
    premises. The registry never constructs a GraphSpec or a host AST.
    """

    pattern_id: str
    form: str
    legacy_pattern: str
    family: str
    applicability: tuple
    state_requirements: tuple
    work_objective: str
    local_relation: str
    version: str = 'local-law-v1'
    catalogue: str = PAPER_CATALOG
    interfaces: tuple = ('rows', 'integer', 'boolean')
    affected_region: str = 'marked_local_rewrite'

    @property
    def identity(self):
        return self.catalogue, self.pattern_id, self.form

    @property
    def native_scalar(self):
        return self.pattern_id in {'P4', 'P5', 'P13', 'S1', 'S2', 'S4', 'S7', 'S8'}

    @property
    def source_interface(self):
        return {'kind': 'rows', 'imports': (), 'adapters': self.interfaces,
                'native_scalar': self.native_scalar,
                'native_requires': 'matching_scalar_type' if self.native_scalar else None}

    @property
    def replacement_interface(self):
        return self.source_interface

    @property
    def local_source(self):
        return {'production': 'build_local_pattern', 'rule': self.identity}

    @property
    def declared_alternative(self):
        return {'rewrite': self.form,
                'direction': 'L_to_H' if self.family == 'short_circuit' else 'symmetry',
                'payload': 'fixed_during_embedding'}

    @property
    def required_features(self):
        # Minimum law requirements. Full host requirements are derived from
        # the instantiated AST before backend compilation.
        return frozenset({'P3': ('bounded_path', 'all'), 'P7': ('multi_label_conjunction',),
                          'P8': ('relationship_alternatives',), 'P11': ('optional_match',),
                          'P12': ('union_all',), 'P13': ('case',),
                          'S3': ('conditional_query',), 'S4': ('case',),
                          'S5': ('row_limit',), 'S6': ('row_limit', 'union_all'),
                          'S7': ('count_subquery',), 'S8': ('exists_subquery', 'union_all'),
                          'S10': ('distinct', 'exists_subquery')}.get(self.pattern_id, ()))

    def build_local(self, options=None, seed=0):
        """Choose a local rule shape, without constructing its outer host."""
        from symbolic import build_local_pattern

        options = dict(options or {})
        if options.get('contexts') or options.get('depth', 0):
            raise ValueError('Host contexts belong to M1, not a local Pattern')
        options.pop('depth', None)
        options['contexts'] = []
        if self.pattern_id == 'P10':
            if options.get('form', self.form) != self.form:
                raise ValueError('Pattern form differs from the selected registry entry')
            options['form'] = self.form
        return build_local_pattern(self.pattern_id, options, seed)

    def to_data(self):
        return {'catalogue': self.catalogue, 'pattern_id': self.pattern_id,
                'form': self.form, 'legacy_pattern': self.legacy_pattern,
                'family': self.family, 'version': self.version,
                'local_source': self.local_source,
                'declared_alternative': self.declared_alternative,
                'required_features': sorted(self.required_features),
                'source_interface': self.source_interface,
                'replacement_interface': self.replacement_interface,
                'applicability': list(self.applicability),
                'state_requirements': list(self.state_requirements),
                'work_objective': self.work_objective,
                'local_relation': self.local_relation,
                'affected_region': self.affected_region}


def _spec(pattern_id, form=None):
    rule = compositional_rule(pattern_id, form)
    short = pattern_id.startswith('S')
    return PatternSpec(pattern_id, rule.form, rule.legacy_pattern,
                       'short_circuit' if short else 'symmetry', _PREMISES[pattern_id],
                       _STATE_NEEDS.get(pattern_id, ('matching_quantity', 'scalar_premises')),
                       'masked_candidate_growth' if short else 'asymmetric_candidate_work',
                       'envelope_equal' if short else 'per_binding_observation_bijection',
                       interfaces=(('rows', 'boolean') if pattern_id in {'P4', 'P5', 'S1', 'S2', 'S8'}
                                   else ('rows', 'integer', 'boolean', 'float', 'string')
                                   if pattern_id in {'P13', 'S4', 'S7'} else ('rows', 'integer', 'boolean')))


class PatternRegistry:
    """Versioned paper catalogue. Alternate P10 forms keep separate identity."""

    version = 'paper-v1/local-law-v1'

    def __init__(self):
        self._specs = {pattern: _spec(pattern) for pattern in COMPOSITION_PATTERNS}

    def __iter__(self):
        return iter(self._specs)

    def __len__(self):
        return len(self._specs)

    def get(self, pattern_id, form=None):
        if pattern_id not in self._specs:
            raise ValueError(f'Unknown paper Pattern {pattern_id}')
        return _spec(pattern_id, form) if form is not None else self._specs[pattern_id]

    def values(self):
        return tuple(self._specs.values())

    def __getitem__(self, pattern_id):
        return self.get(pattern_id)


DEFAULT_REGISTRY = PatternRegistry()


def get_pattern(pattern_id, form=None):
    return DEFAULT_REGISTRY.get(pattern_id, form)
