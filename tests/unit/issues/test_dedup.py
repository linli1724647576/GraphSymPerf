from __future__ import annotations

import copy
from pathlib import Path

import pytest

from graph_sym_perf.issues.dedup import (
    FamilyMatchKind,
    FingerprintSubject,
    IssueFamilyIndex,
    KnownRegressionSignature,
    fingerprint_issue,
    known_regression_signature_from_manifest,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "regressions" / "neo4j_bug001"


def _subject(**changes: object) -> FingerprintSubject:
    values: dict[str, object] = {
        "dbms": "neo4j",
        "dbms_version": "2026.07.0-community",
        "dbms_fingerprint": "c" * 64,
        "canonical_rule_id": "pattern_order_symmetry",
        "reduced_query_a": "MATCH (a:Person {age: 10}), (b:City) RETURN a, b",
        "reduced_query_b": "MATCH (b:City), (a:Person {age: 10}) RETURN a, b",
        "runtime_direction": "b",
        "site_class": "match_pattern_component",
        "submission_path": "relative",
        "reduced_validation_phenotype": "stable-relative",
    }
    values.update(changes)
    return FingerprintSubject(**values)


def _structurally_equivalent_subject(**changes: object) -> FingerprintSubject:
    return _subject(
        reduced_query_a="MATCH (left:Customer {score: 99}), (right:Place) RETURN left, right",
        reduced_query_b="MATCH (right:Place), (left:Customer {score: 99}) RETURN left, right",
        **changes,
    )


def test_exact_signature_binds_dbms_version_rule_raw_pair_order_and_direction() -> None:
    baseline = fingerprint_issue(_subject())

    mutations = (
        _subject(dbms="memgraph"),
        _subject(dbms_version="2026.08.0-community"),
        _subject(dbms_fingerprint="d" * 64),
        _subject(canonical_rule_id="predicate_order_symmetry"),
        _subject(runtime_direction="a"),
        _subject(
            reduced_query_a=_subject().reduced_query_b,
            reduced_query_b=_subject().reduced_query_a,
        ),
    )

    assert baseline.verify()
    assert all(fingerprint_issue(value).exact != baseline.exact for value in mutations)


def test_structural_signature_requires_complete_project_parser_coverage() -> None:
    complete = fingerprint_issue(_subject())
    parse_failure = fingerprint_issue(
        _subject(reduced_query_a="OPTIONAL MATCH (a:Person) RETURN a")
    )
    partial_parse = fingerprint_issue(
        _subject(reduced_query_b="MATCH (a:Person) CALL { RETURN a } RETURN a")
    )

    assert complete.structural is not None
    assert parse_failure.structural is None
    assert partial_parse.structural is None


@pytest.mark.parametrize(
    "unsupported_relationship",
    (
        "[r:KNOWS nonsense]",
        "[r:KNOWS {weight: 10} trailing]",
        "[r:KNOWS*1..3 nonsense]",
        "[r:KNOWS*]",
    ),
)
def test_structural_signature_rejects_partially_parsed_relationship_text(
    unsupported_relationship: str,
) -> None:
    candidate = fingerprint_issue(
        _subject(
            reduced_query_a=(
                f"MATCH (a:Person)-{unsupported_relationship}->(b:Person) RETURN a, b"
            )
        )
    )

    assert candidate.structural is None
    assert candidate.normalized_pair is None


@pytest.mark.parametrize(
    "relationship",
    (
        "[r:KNOWS {weight: 10}]",
        "[:KNOWS*1..3]",
    ),
)
def test_structural_signature_accepts_only_fully_covered_relationship_forms(
    relationship: str,
) -> None:
    query = f"MATCH (a:Person)-{relationship}-(b:Person) RETURN a, b"

    candidate = fingerprint_issue(
        _subject(reduced_query_a=query, reduced_query_b=query)
    )

    assert candidate.structural is not None
    assert candidate.normalized_pair is not None


def test_structural_normalization_abstracts_names_and_literals_only_after_parse() -> None:
    first = fingerprint_issue(_subject())
    changed_names_and_literal = fingerprint_issue(_structurally_equivalent_subject())

    assert first.exact != changed_names_and_literal.exact
    assert first.structural == changed_names_and_literal.structural
    assert first.normalized_pair is not None
    assert "PERSON" not in first.normalized_pair[0]
    assert "AGE" not in first.normalized_pair[0]
    assert "10" not in first.normalized_pair[0]


def test_structural_property_value_variable_survives_alpha_renaming() -> None:
    first_query = (
        "MATCH (b), (c) MATCH (a:Person {peer: b}) RETURN a, b, c"
    )
    renamed_query = (
        "MATCH (left), (right) "
        "MATCH (person:Customer {link: left}) RETURN person, left, right"
    )

    first = fingerprint_issue(
        _subject(reduced_query_a=first_query, reduced_query_b=first_query)
    )
    renamed = fingerprint_issue(
        _subject(reduced_query_a=renamed_query, reduced_query_b=renamed_query)
    )

    assert first.structural is not None
    assert first.structural == renamed.structural


def test_structural_property_value_variable_keeps_its_bound_role() -> None:
    first_query = (
        "MATCH (b), (c) MATCH (a:Person {peer: b}) RETURN a, b, c"
    )
    different_binding = (
        "MATCH (b), (c) MATCH (a:Person {peer: c}) RETURN a, b, c"
    )

    first = fingerprint_issue(
        _subject(reduced_query_a=first_query, reduced_query_b=first_query)
    )
    changed = fingerprint_issue(
        _subject(reduced_query_a=different_binding, reduced_query_b=different_binding)
    )

    assert first.structural is not None
    assert first.structural != changed.structural


def test_query_pair_reversal_is_not_a_structural_merge() -> None:
    first = fingerprint_issue(_subject())
    reversed_pair = fingerprint_issue(
        _subject(
            reduced_query_a=_subject().reduced_query_b,
            reduced_query_b=_subject().reduced_query_a,
        )
    )

    assert first.structural != reversed_pair.structural


def test_exact_and_structural_matches_share_only_one_family() -> None:
    index = IssueFamilyIndex()
    first = index.add(fingerprint_issue(_subject()), issue_id="issue-1")
    exact = index.add(fingerprint_issue(_subject()), issue_id="issue-2")
    structural = index.add(
        fingerprint_issue(_structurally_equivalent_subject()), issue_id="issue-3"
    )

    assert first.is_new_family
    assert first.match_kind is FamilyMatchKind.FRESH
    assert exact.family_id == first.family_id
    assert exact.match_kind is FamilyMatchKind.EXACT
    assert not exact.is_new_family
    assert structural.family_id == first.family_id
    assert structural.match_kind is FamilyMatchKind.STRUCTURAL
    assert not structural.is_new_family


def test_structural_merge_requires_every_conservative_dimension_to_match() -> None:
    baseline = fingerprint_issue(_subject())
    changes = (
        {"canonical_rule_id": "predicate_order_symmetry"},
        {"site_class": "where_boolean"},
        {"runtime_direction": "a"},
        {"submission_path": "absolute"},
        {"reduced_validation_phenotype": "stable-absolute"},
        {"dbms_version": "2026.08.0-community"},
    )

    for index_number, change in enumerate(changes):
        index = IssueFamilyIndex()
        first = index.add(baseline, issue_id=f"first-{index_number}")
        second = index.add(
            fingerprint_issue(_structurally_equivalent_subject(**change)),
            issue_id=f"second-{index_number}",
        )
        assert first.family_id != second.family_id
        assert second.is_new_family


def test_missing_structural_signature_never_auto_merges() -> None:
    index = IssueFamilyIndex()
    first = index.add(
        fingerprint_issue(_subject(reduced_query_a="OPTIONAL MATCH (a) RETURN a")),
        issue_id="issue-1",
    )
    second = index.add(
        fingerprint_issue(
            _subject(
                reduced_query_a="OPTIONAL MATCH (renamed) RETURN renamed",
                reduced_query_b=_subject().reduced_query_b,
            )
        ),
        issue_id="issue-2",
    )

    assert first.family_id != second.family_id
    assert second.is_new_family


def test_family_decision_is_content_bound_and_old_decision_becomes_stale() -> None:
    index = IssueFamilyIndex()
    first = index.add(fingerprint_issue(_subject()), issue_id="issue-1")
    assert first.verify()

    forged = copy.copy(first)
    object.__setattr__(forged, "family_id", "f" * 64)
    assert not forged.verify()

    second = index.add(
        fingerprint_issue(_structurally_equivalent_subject()), issue_id="issue-2"
    )
    assert not first.verify()
    assert second.verify()
    assert first.family_version_hash != second.family_version_hash


def test_replaying_the_original_family_owner_reissues_current_decision() -> None:
    index = IssueFamilyIndex()
    original = index.add(fingerprint_issue(_subject()), issue_id="owner")
    index.add(
        fingerprint_issue(_structurally_equivalent_subject()), issue_id="duplicate"
    )

    replayed = index.reissue_owner(fingerprint_issue(_subject()), issue_id="owner")

    assert not original.verify()
    assert replayed.verify()
    assert replayed.is_new_family
    assert replayed.match_kind is FamilyMatchKind.FRESH
    assert replayed.family_members == (
        ("duplicate", fingerprint_issue(_structurally_equivalent_subject()).exact),
        ("owner", fingerprint_issue(_subject()).exact),
    )


def test_known_bug001_signature_comes_from_strict_task9_manifest_and_is_excluded() -> None:
    known = known_regression_signature_from_manifest(FIXTURES / "manifest.yaml")
    candidate = fingerprint_issue(
        _subject(
            reduced_query_a=(FIXTURES / "fast_query.cypher").read_text(encoding="utf-8").strip(),
            reduced_query_b=(FIXTURES / "slow_query.cypher").read_text(encoding="utf-8").strip(),
            dbms_version="a-current-version-that-may-still-reproduce",
        )
    )
    decision = IssueFamilyIndex(known_regressions=(known,)).add(
        candidate, issue_id="rediscovery"
    )

    assert known.verify()
    assert decision.verify()
    assert decision.known_regression_match
    assert decision.match_kind is FamilyMatchKind.KNOWN_REGRESSION
    assert decision.family_id is None
    assert not decision.is_new_family


def test_known_regression_does_not_match_arbitrary_rule_direction_or_rewritten_pair() -> None:
    known = known_regression_signature_from_manifest(FIXTURES / "manifest.yaml")
    fast = (FIXTURES / "fast_query.cypher").read_text(encoding="utf-8").strip()
    slow = (FIXTURES / "slow_query.cypher").read_text(encoding="utf-8").strip()

    for index_number, candidate in enumerate(
        (
            _subject(reduced_query_a=fast, reduced_query_b=slow, canonical_rule_id="node_order_symmetry"),
            _subject(reduced_query_a=fast, reduced_query_b=slow, runtime_direction="a"),
            _subject(reduced_query_a=fast + " ", reduced_query_b=slow),
        )
    ):
        decision = IssueFamilyIndex(known_regressions=(known,)).add(
            fingerprint_issue(candidate), issue_id=f"fresh-{index_number}"
        )
        assert not decision.known_regression_match
        assert decision.is_new_family


def test_known_regression_signature_cannot_be_publicly_forged_or_tampered() -> None:
    with pytest.raises(TypeError, match="strict Task-9 manifest"):
        KnownRegressionSignature(
            case_id="neo4j_bug001_pattern_order",
            manifest_sha256="a" * 64,
            dbms="neo4j",
            canonical_rule_id="pattern_order_symmetry",
            query_a_hash="b" * 64,
            query_b_hash="c" * 64,
            runtime_direction="b",
            signature_hash="d" * 64,
        )

    issued = known_regression_signature_from_manifest(FIXTURES / "manifest.yaml")
    tampered = copy.copy(issued)
    object.__setattr__(tampered, "query_a_hash", "0" * 64)
    assert not tampered.verify()
    with pytest.raises(ValueError, match="known regression signature"):
        IssueFamilyIndex(known_regressions=(tampered,))
