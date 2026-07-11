"""Executable witnesses for the audited strong-Kleene boolean theorems."""

from __future__ import annotations

from typing import TypeAlias


TruthValue: TypeAlias = bool | None


def kleene_not(value: TruthValue) -> TruthValue:
    return None if value is None else not value


def kleene_and(left: TruthValue, right: TruthValue) -> TruthValue:
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def kleene_or(left: TruthValue, right: TruthValue) -> TruthValue:
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


def constant_true_disjunction(value: TruthValue) -> TruthValue:
    return kleene_or(value, True)


def constant_false_conjunction(value: TruthValue) -> TruthValue:
    return kleene_and(value, False)


def null_partition_tautology(value: TruthValue) -> bool:
    return (value is True) or (value is False) or (value is None)


def null_contradiction(value: TruthValue) -> bool:
    return (value is True) and (value is False)


def null_complete_excluded_middle(value: TruthValue) -> bool:
    return kleene_or(kleene_or(value, kleene_not(value)), value is None) is True


def double_negation(value: TruthValue) -> TruthValue:
    return kleene_not(kleene_not(value))


def de_morgan_or(left: TruthValue, right: TruthValue) -> bool:
    return kleene_not(kleene_or(left, right)) == kleene_and(kleene_not(left), kleene_not(right))


def de_morgan_and(left: TruthValue, right: TruthValue) -> bool:
    return kleene_not(kleene_and(left, right)) == kleene_or(kleene_not(left), kleene_not(right))


__all__ = [
    "TruthValue",
    "constant_false_conjunction",
    "constant_true_disjunction",
    "de_morgan_and",
    "de_morgan_or",
    "double_negation",
    "kleene_and",
    "kleene_not",
    "kleene_or",
    "null_complete_excluded_middle",
    "null_contradiction",
    "null_partition_tautology",
]
