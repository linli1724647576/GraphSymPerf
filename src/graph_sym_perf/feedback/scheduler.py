"""Deterministic feedback-guided transform selection."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from graph_sym_perf.core import CaseRecord, FeedbackConfig, QueryCase, RunConfig, TransformConfig, is_profile_supported_level
from graph_sym_perf.coverage import PMCEvidence
from graph_sym_perf.feedback.model import FeedbackDecision, FeedbackState, RuleKey, RuleStats
from graph_sym_perf.feedback.scoring import compute_reward, rule_priority
from graph_sym_perf.feedback.state import family_rules
from graph_sym_perf.transforms.registry import select_family_name


class TransformScheduler:
    def __init__(self, config: TransformConfig, feedback_config: FeedbackConfig, state: FeedbackState):
        self.config = config
        self.feedback_config = feedback_config
        self.state = state

    def choose_for_case(self, query_case: QueryCase) -> FeedbackDecision:
        pinned = self._pinned_decision(query_case)
        if pinned is not None:
            return pinned
        eligible_families = self._eligible_families(query_case)
        if not eligible_families:
            family = select_family_name(query_case, self.config)
            rules = family_rules(self.config, family)
            return FeedbackDecision(
                family=family,
                enabled_rules=self._cap_rules(rules),
                reason="fallback_to_default_family",
                scores={rule: 0.0 for rule in self._cap_rules(rules)},
            )
        if not self.feedback_config.enabled:
            family = select_family_name(query_case, replace(self.config, enabled_families=eligible_families))
            rules = self._static_rule_order(query_case, family_rules(self.config, family), family)
            return FeedbackDecision(
                family=family,
                enabled_rules=self._cap_rules(rules),
                reason="feedback_disabled_static_order",
                scores={rule: 0.0 for rule in self._cap_rules(rules)},
                metadata={"eligible_families": eligible_families},
            )

        family = self._choose_family(query_case, eligible_families)
        ordered = self._rank_rules(query_case, family)
        capped = self._cap_rules(ordered)
        return FeedbackDecision(
            family=family,
            enabled_rules=capped,
            reason="feedback_guided_selection",
            scores={rule: self._rule_score(family, rule) for rule in capped},
            metadata={"eligible_families": eligible_families},
        )

    def update_from_case_record(self, record: CaseRecord, pmc_evidence: PMCEvidence | None = None) -> None:
        """Update per-rule statistics from a completed case record.

        Invariants (document 4.md §P0-2):
          - attempted >= generated  (skipped variants are attempted but not generated)
          - equivalence_passed + equivalence_failed <= generated
          - generated + skipped <= attempted
          - Case-level error/timeout state MUST NOT propagate to individual rule variants.
        """
        candidate = bool(record.oracle and record.oracle.is_candidate)
        strong_candidate = bool(record.confirmation and is_profile_supported_level(record.confirmation.support_level))
        equivalences_by_id = {item.case_id: item for item in record.equivalences}
        pmc_rule = pmc_evidence.transform_rule if pmc_evidence is not None else None
        family_name = self._infer_family(record)

        # -- generated variants (passed static safety check) --
        for variant in record.family.variants:
            rule = variant.transform_rule
            stats = self._stats(family_name, rule)
            stats.attempted += 1
            stats.generated += 1

            equivalence = equivalences_by_id.get(variant.variant_id)
            if equivalence is not None:
                if equivalence.passed:
                    stats.equivalence_passed += 1
                else:
                    stats.equivalence_failed += 1
                    # Attribute variant-level (not case-level) errors and timeouts.
                    _reason = str(equivalence.reason or "")
                    if "QUERY_ERROR" in _reason.upper():
                        stats.errors += 1
                    if "TIMEOUT" in _reason.upper():
                        stats.timeouts += 1

            # PMC-linked stats: only for the specific rule that produced the evidence.
            if record.confirmation is not None and pmc_rule == rule:
                stats.profile_available += 1
                stats.pmc_units += pmc_evidence.unit_count if pmc_evidence else 0
                stats.pmc_new_units += pmc_evidence.new_unit_count if pmc_evidence else 0
                if candidate:
                    stats.candidate_count += 1
                if strong_candidate:
                    stats.strong_candidate_count += 1

            stats.reward = compute_reward(stats, self.feedback_config)
            self._merge_family_stats(stats)

        # -- skipped variants (failed static safety check) --
        for variant in record.family.skipped_variants:
            rule = variant.transform_rule
            stats = self._stats(family_name, rule)
            stats.attempted += 1
            stats.skipped += 1
            if variant.skip_reason and "TIMEOUT" in str(variant.skip_reason).upper():
                stats.timeouts += 1
            stats.reward = compute_reward(stats, self.feedback_config)
            self._merge_family_stats(stats)

    def _eligible_families(self, query_case: QueryCase) -> tuple[str, ...]:
        eligible: list[str] = []
        for family in self.config.enabled_families:
            if family == "bool_redundancy" and query_case.source != "gdsmith" and not query_case.base_predicate:
                continue
            eligible.append(family)
        return tuple(eligible)

    def _pinned_decision(self, query_case: QueryCase) -> FeedbackDecision | None:
        family = query_case.transform_family or self._family_for_rule(query_case.transform_rule)
        if family is None:
            return None
        if query_case.transform_rule:
            rules = (query_case.transform_rule,)
        else:
            rules = self._cap_rules(family_rules(self.config, family))
        return FeedbackDecision(
            family=family,
            enabled_rules=rules,
            reason="case_pinned_transform",
            scores={rule: 0.0 for rule in rules},
        )

    @staticmethod
    def _family_for_rule(rule: str | None) -> str | None:
        if rule is None:
            return None
        if rule in family_rules(TransformConfig(), "symmetry"):
            return "symmetry"
        if rule in family_rules(TransformConfig(), "bool_redundancy"):
            return "bool_redundancy"
        return None

    def _choose_family(self, query_case: QueryCase, eligible_families: tuple[str, ...]) -> str:
        if len(eligible_families) == 1:
            return eligible_families[0]
        if all(self.state.family_stats[family].attempted == 0 for family in eligible_families):
            index = self._stable_choice_index(query_case.case_id, len(eligible_families))
            return eligible_families[index]
        if self._should_explore(query_case.case_id):
            index = self._stable_choice_index(f"explore:{query_case.case_id}", len(eligible_families))
            return eligible_families[index]
        ranked = sorted(
            eligible_families,
            key=lambda family: (
                -self.state.family_stats[family].reward,
                family,
            ),
        )
        return ranked[0]

    def _rank_rules(self, query_case: QueryCase, family: str) -> tuple[str, ...]:
        rules = family_rules(self.config, family)
        if not rules:
            return ()
        if self._should_explore(query_case.case_id):
            return tuple(sorted(rules, key=lambda rule: self._stable_token(f"{family}:{rule}:{query_case.case_id}")))
        return tuple(
            rule
            for rule, _ in sorted(
                ((rule, self._rule_score(family, rule)) for rule in rules),
                key=lambda item: (-item[1], item[0]),
            )
        )

    def _static_rule_order(self, query_case: QueryCase, rules: tuple[str, ...], family: str) -> tuple[str, ...]:
        if len(rules) <= 1:
            return rules
        if self.config.max_variants_per_case is None or self.config.max_variants_per_case >= len(rules):
            return rules
        offset = self._stable_token(f"static-rotation:{self.state.seed}:{family}:{query_case.case_id}") % len(rules)
        return rules[offset:] + rules[:offset]

    def _cap_rules(self, rules: tuple[str, ...]) -> tuple[str, ...]:
        minimum = 1 if not self.feedback_config.enabled else max(1, self.feedback_config.min_rules_per_case)
        maximum = self.config.max_variants_per_case
        if self.feedback_config.enabled and self.feedback_config.max_rules_per_case is not None:
            maximum = self.feedback_config.max_rules_per_case
        capped = rules[: maximum] if maximum is not None else rules
        return capped[: max(minimum, len(capped))] if len(capped) < minimum else capped

    def _rule_score(self, family: str, rule: str) -> float:
        stats = self._stats(family, rule)
        stats.reward = compute_reward(stats, self.feedback_config)
        reward, novelty_rate, equivalence_rate, candidate_density, failure_penalty, attempted_penalty = rule_priority(stats)
        return round(
            reward + novelty_rate * 5.0 + equivalence_rate * 2.0 + candidate_density * 4.0 + failure_penalty * 0.01 + attempted_penalty * 0.001,
            4,
        )

    def _stats(self, family: str, rule: str) -> RuleStats:
        key = RuleKey(family=family, rule=rule)
        if key not in self.state.rule_stats:
            self.state.rule_stats[key] = RuleStats(family=family, rule=rule)
        if family not in self.state.family_stats:
            self.state.family_stats[family] = RuleStats(family=family, rule="__family__")
        return self.state.rule_stats[key]

    def _merge_family_stats(self, stats: RuleStats) -> None:
        family_stats = self.state.family_stats.setdefault(stats.family, RuleStats(family=stats.family, rule="__family__"))
        family_stats.attempted = sum(item.attempted for key, item in self.state.rule_stats.items() if key.family == stats.family)
        family_stats.generated = sum(item.generated for key, item in self.state.rule_stats.items() if key.family == stats.family)
        family_stats.skipped = sum(item.skipped for key, item in self.state.rule_stats.items() if key.family == stats.family)
        family_stats.equivalence_passed = sum(
            item.equivalence_passed for key, item in self.state.rule_stats.items() if key.family == stats.family
        )
        family_stats.equivalence_failed = sum(
            item.equivalence_failed for key, item in self.state.rule_stats.items() if key.family == stats.family
        )
        family_stats.profile_available = sum(
            item.profile_available for key, item in self.state.rule_stats.items() if key.family == stats.family
        )
        family_stats.candidate_count = sum(
            item.candidate_count for key, item in self.state.rule_stats.items() if key.family == stats.family
        )
        family_stats.strong_candidate_count = sum(
            item.strong_candidate_count for key, item in self.state.rule_stats.items() if key.family == stats.family
        )
        family_stats.pmc_units = sum(item.pmc_units for key, item in self.state.rule_stats.items() if key.family == stats.family)
        family_stats.pmc_new_units = sum(
            item.pmc_new_units for key, item in self.state.rule_stats.items() if key.family == stats.family
        )
        family_stats.timeouts = sum(item.timeouts for key, item in self.state.rule_stats.items() if key.family == stats.family)
        family_stats.errors = sum(item.errors for key, item in self.state.rule_stats.items() if key.family == stats.family)
        family_stats.reward = compute_reward(family_stats, self.feedback_config)

    def _should_explore(self, token: str) -> bool:
        if self.feedback_config.exploration_rate <= 0:
            return False
        ratio = (self._stable_token(f"explore:{self.state.seed}:{token}") % 10_000) / 10_000.0
        return ratio < self.feedback_config.exploration_rate

    def _stable_choice_index(self, token: str, size: int) -> int:
        if size <= 1:
            return 0
        return self._stable_token(f"choice:{self.state.seed}:{token}") % size

    @staticmethod
    def _stable_token(token: str) -> int:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return int(digest[:16], 16)

    @staticmethod
    def _infer_family(record: CaseRecord) -> str:
        variants = record.family.variants or record.family.skipped_variants
        if variants:
            return str(variants[0].metadata.get("transform_family") or "symmetry")
        return "symmetry"
