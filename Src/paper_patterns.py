"""Paper identifiers for the supported compositional rules.

Historical mutation/state recipes retain their identifiers on replay. New
generation records paper identity separately from that compatibility mapping.
This registry selects a local query law; it never selects a graph recipe.
"""

from dataclasses import dataclass


PAPER_CATALOG = "paper-v1"
LEGACY_CATALOG = "legacy-v1"


@dataclass(frozen=True)
class Rule:
    paper_pattern: str
    legacy_pattern: str
    form: str


_RULES = {
    "P1": Rule("P1", "P1", "edge_mirror"),
    "P2": Rule("P2", "P2", "inverse_edge"),
    "P3": Rule("P3", "P3", "bounded_path"),
    "P4": Rule("P4", "P4", "and"),
    "P5": Rule("P5", "P5", "or"),
    "P6": Rule("P6", "P6", "equality"),
    "P7": Rule("P7", "P7", "labels"),
    "P8": Rule("P8", "P8", "types"),
    "P9": Rule("P9", "P9", "predicate_placement"),
    "P10": Rule("P10", "P11", "shared_anchor"),
    "P11": Rule("P11", "P13", "optional_branches"),
    "P12": Rule("P12", "P14", "union_all"),
    "P13": Rule("P13", "P15", "case_dual"),
    "S1": Rule("S1", "S1", "absorbing_or"),
    "S2": Rule("S2", "S2", "absorbing_and"),
    "S3": Rule("S3", "S3", "conditional"),
    "S4": Rule("S4", "S4", "case_guard"),
    "S5": Rule("S5", "S5", "empty_limit"),
    "S6": Rule("S6", "S6", "bounded_demand"),
    "S7": Rule("S7", "S7", "coalesce"),
    "S8": Rule("S8", "S8", "existential_witness"),
    "S9": Rule("S9", "S9", "empty_input"),
    "S10": Rule("S10", "S10", "membership"),
}
COMPOSITION_PATTERNS = tuple(_RULES)


def compositional_rule(pattern: str, form: str | None = None) -> Rule:
    if not isinstance(pattern, str) or form is not None and not isinstance(form, str):
        raise ValueError("Paper pattern and form require string identifiers")
    if pattern not in _RULES:
        raise ValueError(f"Paper pattern {pattern} is not supported by compositional generation")
    rule = _RULES[pattern]
    if pattern == "P10" and form in {"independent", "chain"}:
        return Rule(pattern, "P10" if form == "independent" else "P12", form)
    if form is not None and form != rule.form:
        raise ValueError(f"Paper pattern {pattern} currently supports form {rule.form}")
    return rule
