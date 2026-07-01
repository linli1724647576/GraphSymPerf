"""Deterministic feedback-guided transform selection."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from graph_sym_perf.core import CaseRecord, FeedbackConfig, QueryCase, RunConfig, TransformConfig
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
            rules = family_rules(self.config, family)
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
        candidate = bool(record.oracle and record.oracle.is_candidate)
        strong_candidate = bool(record.confirmation and record.confirmation.support_level == "profile_supported")
        passed_ids = {item.case_id for item in record.equivalences if item.passed}
        generated_rules = {variant.transform_rule for variant in record.family.variants}
        skipped_rules = {variant.transform_rule for variant in record.family.skipped_variants}
        pmc_rule = pmc_evidence.transform_rule if pmc_evidence is not None else None
        family_name = self._infer_family(record)
        for rule in sorted(generated_rules | skipped_rules):
            stats = self._stats(family_name, rule)
            stats.attempted += 1
            if rule in generated_rules:
                stats.generated += 1
            if rule in skipped_rules:
                stats.skipped += 1
            if any(item.case_id in passed_ids and f"_{rule}" in item.case_id for item in record.equivalences):
                stats.equivalence_passed += 1
            elif record.equivalences:
                stats.equivalence_failed += 1
            if record.confirmation is not None and pmc_rule == rule:
                stats.profile_available += 1
                stats.pmc_units += pmc_evidence.unit_count if pmc_evidence else 0
                stats.pmc_new_units += pmc_evidence.new_unit_count if pmc_evidence else 0
                if candidate:
                    stats.candidate_count += 1
                if strong_candidate:
                    stats.strong_candidate_count += 1
            if record.state == "error":
                stats.errors += 1
            if "TIMEOUT" in str(record.reason).upper():
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
