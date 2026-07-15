"""Parser-proven, conservative issue-family fingerprints.

Structural signatures are deliberately optional.  The normalizer runs only
after the project parser has proved complete coverage of a small read-only
fragment; unsupported syntax never falls back to textual guessing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from collections.abc import Iterable, Mapping

from graph_sym_perf.core import stable_json_dumps
from graph_sym_perf.cypher.ast import EdgePattern, NodePattern, PatternList, VarLengthPathPattern
from graph_sym_perf.cypher.parser import CypherParseError, parse_match_clause, parse_query, split_top_level_commas
from graph_sym_perf.reducer.dependencies import analyze_query


_FINGERPRINT_VERSION = "issue-fingerprint-v1"
_FAMILY_VERSION = "issue-family-v1"
_DURABLE_FAMILY_PROOF_SCHEMA = "durable-family-proof-v1"
_DURABLE_FAMILY_PROOF_REF = {
    "schema": _DURABLE_FAMILY_PROOF_SCHEMA,
    "registry_rel": "issues/registry.jsonl",
    "review_rel": "issues/dedup_review.jsonl",
}
_DECISION_TOKEN = object()
_KNOWN_REGRESSION_TOKEN = object()
_SUPPORTED_RELATIONSHIP_RE = re.compile(
    r"^\[\s*"
    r"(?:(?P<variable>[A-Za-z]\w*)\s*)?"
    r"(?P<types>(?::\s*[A-Za-z]\w*\s*)*)"
    r"(?P<length>\*\s*\d+\s*\.\.\s*\d+\s*)?"
    r"(?P<properties>\{[^{}]*\}\s*)?"
    r"\]$",
    re.S,
)


class FamilyMatchKind(str, Enum):
    FRESH = "fresh"
    EXACT = "exact"
    STRUCTURAL = "structural"
    KNOWN_REGRESSION = "known_regression"


@dataclass(frozen=True)
class FingerprintSubject:
    dbms: str
    dbms_version: str
    dbms_fingerprint: str
    graph_snapshot_hash: str
    canonical_rule_id: str
    reduced_query_a: str
    reduced_query_b: str
    runtime_direction: str
    site_class: str
    submission_path: str
    reduced_validation_phenotype: str

    def __post_init__(self) -> None:
        required = {
            "dbms": self.dbms,
            "dbms_version": self.dbms_version,
            "dbms_fingerprint": self.dbms_fingerprint,
            "graph_snapshot_hash": self.graph_snapshot_hash,
            "canonical_rule_id": self.canonical_rule_id,
            "reduced_query_a": self.reduced_query_a,
            "reduced_query_b": self.reduced_query_b,
            "site_class": self.site_class,
            "submission_path": self.submission_path,
            "reduced_validation_phenotype": self.reduced_validation_phenotype,
        }
        missing = tuple(name for name, value in required.items() if not isinstance(value, str) or not value.strip())
        if missing:
            raise ValueError(f"missing fingerprint field(s): {', '.join(missing)}")
        if self.runtime_direction not in {"a", "b"}:
            raise ValueError("runtime_direction must be 'a' or 'b'")


@dataclass(frozen=True)
class IssueFingerprint:
    dbms: str
    dbms_version: str
    dbms_fingerprint: str
    graph_snapshot_hash: str
    canonical_rule_id: str
    reduced_query_a: str
    reduced_query_b: str
    query_a_hash: str
    query_b_hash: str
    runtime_direction: str
    site_class: str
    submission_path: str
    reduced_validation_phenotype: str
    exact: str
    structural: str | None
    normalized_pair: tuple[str, str] | None
    fingerprint_hash: str

    def verify(self) -> bool:
        required_strings = (
            self.dbms,
            self.dbms_version,
            self.dbms_fingerprint,
            self.graph_snapshot_hash,
            self.canonical_rule_id,
            self.reduced_query_a,
            self.reduced_query_b,
            self.query_a_hash,
            self.query_b_hash,
            self.runtime_direction,
            self.site_class,
            self.submission_path,
            self.reduced_validation_phenotype,
            self.exact,
            self.fingerprint_hash,
        )
        if (
            any(not isinstance(value, str) for value in required_strings)
            or not self.reduced_query_a.strip()
            or not self.reduced_query_b.strip()
            or self.structural is not None
            and not isinstance(self.structural, str)
            or self.normalized_pair is not None
            and (
                not isinstance(self.normalized_pair, tuple)
                or len(self.normalized_pair) != 2
                or any(not isinstance(value, str) for value in self.normalized_pair)
            )
        ):
            return False
        query_a_hash = _query_hash(self.reduced_query_a)
        query_b_hash = _query_hash(self.reduced_query_b)
        normalized_a = _normalize_parser_proven_query(self.reduced_query_a)
        normalized_b = _normalize_parser_proven_query(self.reduced_query_b)
        normalized_pair = (
            (normalized_a, normalized_b)
            if normalized_a is not None and normalized_b is not None
            else None
        )
        exact = _exact_signature(
            dbms=self.dbms,
            dbms_version=self.dbms_version,
            dbms_fingerprint=self.dbms_fingerprint,
            canonical_rule_id=self.canonical_rule_id,
            query_a_hash=query_a_hash,
            query_b_hash=query_b_hash,
            runtime_direction=self.runtime_direction,
        )
        structural = _structural_signature(
            dbms=self.dbms,
            dbms_version=self.dbms_version,
            dbms_fingerprint=self.dbms_fingerprint,
            canonical_rule_id=self.canonical_rule_id,
            site_class=self.site_class,
            normalized_pair=normalized_pair,
            runtime_direction=self.runtime_direction,
        )
        return bool(
            self.runtime_direction in {"a", "b"}
            and re.fullmatch(r"[0-9a-f]{64}", self.graph_snapshot_hash) is not None
            and query_a_hash == self.query_a_hash
            and query_b_hash == self.query_b_hash
            and normalized_pair == self.normalized_pair
            and exact == self.exact
            and structural == self.structural
            and self.fingerprint_hash == _fingerprint_hash(self, exact=exact, structural=structural)
        )


@dataclass(frozen=True, init=False)
class KnownRegressionSignature:
    case_id: str
    manifest_sha256: str
    dbms: str
    canonical_rule_id: str
    graph_snapshot_hash: str
    query_a_hash: str
    query_b_hash: str
    runtime_direction: str
    signature_hash: str
    _issued: object

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("KnownRegressionSignature requires the strict Task-9 manifest factory")

    @classmethod
    def _from_manifest(
        cls,
        *,
        case_id: str,
        manifest_sha256: str,
        dbms: str,
        canonical_rule_id: str,
        graph_snapshot_hash: str,
        query_a_hash: str,
        query_b_hash: str,
        runtime_direction: str,
        token: object,
    ) -> "KnownRegressionSignature":
        if token is not _KNOWN_REGRESSION_TOKEN:
            raise TypeError("KnownRegressionSignature requires the strict Task-9 manifest factory")
        instance = object.__new__(cls)
        values = {
            "case_id": case_id,
            "manifest_sha256": manifest_sha256,
            "dbms": dbms,
            "canonical_rule_id": canonical_rule_id,
            "graph_snapshot_hash": graph_snapshot_hash,
            "query_a_hash": query_a_hash,
            "query_b_hash": query_b_hash,
            "runtime_direction": runtime_direction,
        }
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        object.__setattr__(instance, "_issued", _KNOWN_REGRESSION_TOKEN)
        object.__setattr__(instance, "signature_hash", _known_signature_hash(instance))
        return instance

    def verify(self) -> bool:
        return bool(
            self._issued is _KNOWN_REGRESSION_TOKEN
            and self.case_id == "neo4j_bug001_pattern_order"
            and self.dbms == "neo4j"
            and self.canonical_rule_id == "pattern_order_symmetry"
            and self.runtime_direction == "b"
            and self.signature_hash == _known_signature_hash(self)
        )

    def matches(self, fingerprint: IssueFingerprint) -> bool:
        return bool(
            self.verify()
            and fingerprint.verify()
            and (
                fingerprint.dbms,
                fingerprint.canonical_rule_id,
                fingerprint.graph_snapshot_hash,
                fingerprint.query_a_hash,
                fingerprint.query_b_hash,
                fingerprint.runtime_direction,
            )
            == (
                self.dbms,
                self.canonical_rule_id,
                self.graph_snapshot_hash,
                self.query_a_hash,
                self.query_b_hash,
                self.runtime_direction,
            )
        )


@dataclass(frozen=True, init=False)
class IssueFamilyDecision:
    issue_id: str
    owner_issue_id: str | None
    family_id: str | None
    fingerprint: IssueFingerprint
    match_kind: FamilyMatchKind
    matched_issue_id: str | None
    known_regression_match: bool
    known_regression_case_id: str | None
    is_new_family: bool
    family_members: tuple[tuple[str, str], ...]
    family_fingerprints: tuple[tuple[str, IssueFingerprint], ...]
    member_count: int
    member_proof_root: str
    family_version_hash: str
    decision_hash: str
    _active: bool

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("IssueFamilyDecision can only be issued by IssueFamilyIndex")

    @classmethod
    def _from_index(
        cls,
        *,
        issue_id: str,
        owner_issue_id: str | None,
        family_id: str | None,
        fingerprint: IssueFingerprint,
        match_kind: FamilyMatchKind,
        matched_issue_id: str | None,
        known_regression_match: bool,
        known_regression_case_id: str | None,
        is_new_family: bool,
        family_members: tuple[tuple[str, str], ...],
        family_fingerprints: tuple[tuple[str, IssueFingerprint], ...],
        token: object,
    ) -> "IssueFamilyDecision":
        if token is not _DECISION_TOKEN:
            raise TypeError("IssueFamilyDecision can only be issued by IssueFamilyIndex")
        instance = object.__new__(cls)
        member_proof_root = family_member_proof_root(family_fingerprints)
        member_count = len(family_fingerprints)
        version_hash = _family_version_hash(
            family_id,
            family_members,
            family_fingerprints,
            known_regression_case_id,
        )
        fields: dict[str, object] = {
            "issue_id": issue_id,
            "owner_issue_id": owner_issue_id,
            "family_id": family_id,
            "fingerprint_hash": fingerprint.fingerprint_hash,
            "match_kind": match_kind.value,
            "matched_issue_id": matched_issue_id,
            "known_regression_match": known_regression_match,
            "known_regression_case_id": known_regression_case_id,
            "is_new_family": is_new_family,
            "member_count": member_count,
            "member_proof_root": member_proof_root,
            "family_version_hash": version_hash,
        }
        for name, value in {
            "issue_id": issue_id,
            "owner_issue_id": owner_issue_id,
            "family_id": family_id,
            "fingerprint": fingerprint,
            "match_kind": match_kind,
            "matched_issue_id": matched_issue_id,
            "known_regression_match": known_regression_match,
            "known_regression_case_id": known_regression_case_id,
            "is_new_family": is_new_family,
            "family_members": family_members,
            "family_fingerprints": family_fingerprints,
            "member_count": member_count,
            "member_proof_root": member_proof_root,
            "family_version_hash": version_hash,
            "decision_hash": _hash_payload("family-decision-v1", fields),
            "_active": True,
        }.items():
            object.__setattr__(instance, name, value)
        return instance

    def verify(self) -> bool:
        fields: dict[str, object] = {
            "issue_id": self.issue_id,
            "owner_issue_id": self.owner_issue_id,
            "family_id": self.family_id,
            "fingerprint_hash": self.fingerprint.fingerprint_hash,
            "match_kind": self.match_kind.value,
            "matched_issue_id": self.matched_issue_id,
            "known_regression_match": self.known_regression_match,
            "known_regression_case_id": self.known_regression_case_id,
            "is_new_family": self.is_new_family,
            "member_count": self.member_count,
            "member_proof_root": self.member_proof_root,
            "family_version_hash": self.family_version_hash,
        }
        known_invariants = (
            self.family_id is None
            and self.match_kind is FamilyMatchKind.KNOWN_REGRESSION
            and self.known_regression_case_id == "neo4j_bug001_pattern_order"
            and self.owner_issue_id is None
            and not self.is_new_family
        )
        fresh_invariants = (
            self.family_id is not None
            and self.match_kind is not FamilyMatchKind.KNOWN_REGRESSION
            and not self.known_regression_match
            and self.known_regression_case_id is None
            and bool(self.family_members)
            and self.owner_issue_id is not None
            and any(member == self.owner_issue_id for member, _exact in self.family_members)
            and {member for member, _exact in self.family_members}
            == {member for member, _fingerprint in self.family_fingerprints}
            and all(
                dict(self.family_members).get(member) == member_fingerprint.exact
                for member, member_fingerprint in self.family_fingerprints
            )
            and any(
                member == self.issue_id
                and member_fingerprint == self.fingerprint
                for member, member_fingerprint in self.family_fingerprints
            )
            and all(member_fingerprint.verify() for _member, member_fingerprint in self.family_fingerprints)
            and self.family_id
            == min(_new_family_id(member_fingerprint) for _member, member_fingerprint in self.family_fingerprints)
            and self.owner_issue_id
            == min(
                self.family_fingerprints,
                key=lambda item: (_new_family_id(item[1]), item[0]),
            )[0]
            and (self.is_new_family == (self.match_kind is FamilyMatchKind.FRESH))
        )
        return bool(
            self._active
            and self.issue_id.strip()
            and self.fingerprint.verify()
            and (known_invariants if self.known_regression_match else fresh_invariants)
            and self.family_version_hash
            == _family_version_hash(
                self.family_id,
                self.family_members,
                self.family_fingerprints,
                self.known_regression_case_id,
            )
            and self.member_count == len(self.family_fingerprints)
            and self.member_proof_root
            == family_member_proof_root(self.family_fingerprints)
            and self.decision_hash == _hash_payload("family-decision-v1", fields)
        )

    def _invalidate(self) -> None:
        object.__setattr__(self, "_active", False)


class IssueFamilyIndex:
    """A deterministic, conservative in-memory index used under the durable lock."""

    def __init__(self, *, known_regressions: Iterable[KnownRegressionSignature] = ()) -> None:
        self._known_regressions = tuple(known_regressions)
        if any(not signature.verify() for signature in self._known_regressions):
            raise ValueError("known regression signature is invalid")
        self._exact: dict[str, str] = {}
        self._structural: dict[tuple[str, str, str], str] = {}
        self._members: dict[str, dict[str, str]] = {}
        self._owners: dict[str, str] = {}
        self._fingerprints: dict[str, IssueFingerprint] = {}
        self._issue_families: dict[str, str] = {}
        self._family_seeds: dict[str, set[str]] = {}
        self._decisions: dict[str, list[IssueFamilyDecision]] = {}

    def add(self, fingerprint: IssueFingerprint, *, issue_id: str) -> IssueFamilyDecision:
        if not isinstance(fingerprint, IssueFingerprint) or not fingerprint.verify():
            raise ValueError("family index requires an intact IssueFingerprint")
        if not issue_id.strip():
            raise ValueError("family index requires issue_id")
        for known in self._known_regressions:
            if known.matches(fingerprint):
                return IssueFamilyDecision._from_index(
                    issue_id=issue_id,
                    owner_issue_id=None,
                    family_id=None,
                    fingerprint=fingerprint,
                    match_kind=FamilyMatchKind.KNOWN_REGRESSION,
                    matched_issue_id=None,
                    known_regression_match=True,
                    known_regression_case_id=known.case_id,
                    is_new_family=False,
                    family_members=((issue_id, fingerprint.exact),),
                    family_fingerprints=(
                        (issue_id, fingerprint),
                    ),
                    token=_DECISION_TOKEN,
                )

        existing_fingerprint = self._fingerprints.get(issue_id)
        if existing_fingerprint is not None and existing_fingerprint != fingerprint:
            raise ValueError("issue_id is already bound to a different fingerprint")

        matched_issue_id: str | None = None
        exact_family_id = self._exact.get(fingerprint.exact)
        structural_key = _structural_family_key(fingerprint)
        structural_family_id = (
            self._structural.get(structural_key) if structural_key is not None else None
        )
        family_id = exact_family_id
        match_kind = FamilyMatchKind.EXACT if family_id is not None else FamilyMatchKind.FRESH
        if exact_family_id is not None:
            matched_issue_id = _first_matching_issue(
                self._members[exact_family_id], fingerprint.exact
            )
        if family_id is None and structural_key is not None:
            family_id = structural_family_id
            if family_id is not None:
                match_kind = FamilyMatchKind.STRUCTURAL
                matched_issue_id = min(self._members[family_id])

        matched_families = {
            candidate
            for candidate in (exact_family_id, structural_family_id)
            if candidate is not None
        }
        seed = _new_family_id(fingerprint)
        if not matched_families:
            family_id = seed
            self._members[family_id] = {}
            self._decisions[family_id] = []
            self._family_seeds[family_id] = {seed}
            self._owners[family_id] = issue_id
        else:
            family_id = self._merge_connected_families(matched_families, seed=seed)

        members = self._members[family_id]
        previous_exact = members.get(issue_id)
        changed = previous_exact is None
        members[issue_id] = fingerprint.exact
        self._fingerprints[issue_id] = fingerprint
        self._issue_families[issue_id] = family_id
        self._family_seeds[family_id].add(seed)
        self._owners[family_id] = min(
            members,
            key=lambda member: (_new_family_id(self._fingerprints[member]), member),
        )
        self._rebuild_match_indexes()
        if changed:
            for decision in self._decisions[family_id]:
                decision._invalidate()
        frozen_members = tuple(sorted(members.items()))
        frozen_fingerprints = self._family_fingerprint_proof(family_id)
        decision = IssueFamilyDecision._from_index(
            issue_id=issue_id,
            owner_issue_id=self._owners[family_id],
            family_id=family_id,
            fingerprint=fingerprint,
            match_kind=match_kind,
            matched_issue_id=matched_issue_id,
            known_regression_match=False,
            known_regression_case_id=None,
            is_new_family=match_kind is FamilyMatchKind.FRESH,
            family_members=frozen_members,
            family_fingerprints=frozen_fingerprints,
            token=_DECISION_TOKEN,
        )
        self._decisions[family_id].append(decision)
        return decision

    def _family_fingerprint_proof(
        self,
        family_id: str,
    ) -> tuple[tuple[str, IssueFingerprint], ...]:
        return tuple(
            sorted(
                (
                    issue_id,
                    self._fingerprints[issue_id],
                )
                for issue_id in self._members[family_id]
            )
        )

    def _merge_connected_families(self, family_ids: set[str], *, seed: str) -> str:
        """Union all exact/structural components under one canonical identity."""

        canonical_id = min((*family_ids, seed))
        if family_ids == {canonical_id}:
            self._family_seeds[canonical_id].add(seed)
            return canonical_id

        merged_members: dict[str, str] = {}
        merged_decisions: list[IssueFamilyDecision] = []
        merged_seeds = {seed}
        owners: list[str] = []
        for family_id in sorted(family_ids):
            merged_members.update(self._members[family_id])
            merged_decisions.extend(self._decisions[family_id])
            merged_seeds.update(self._family_seeds[family_id])
            owners.append(self._owners[family_id])
            for decision in self._decisions[family_id]:
                decision._invalidate()
        for family_id in family_ids:
            del self._members[family_id]
            del self._decisions[family_id]
            del self._family_seeds[family_id]
            del self._owners[family_id]
        self._members[canonical_id] = merged_members
        self._decisions[canonical_id] = merged_decisions
        self._family_seeds[canonical_id] = merged_seeds
        self._owners[canonical_id] = min(
            owners,
            key=lambda member: (_new_family_id(self._fingerprints[member]), member),
        )
        for member in merged_members:
            self._issue_families[member] = canonical_id
        return canonical_id

    def _rebuild_match_indexes(self) -> None:
        exact: dict[str, str] = {}
        structural: dict[tuple[str, str, str], str] = {}
        for issue_id, fingerprint in self._fingerprints.items():
            family_id = self._issue_families[issue_id]
            previous_exact = exact.setdefault(fingerprint.exact, family_id)
            if previous_exact != family_id:
                raise ValueError("exact signature spans multiple issue families")
            structural_key = _structural_family_key(fingerprint)
            if structural_key is not None:
                previous_structural = structural.setdefault(structural_key, family_id)
                if previous_structural != family_id:
                    raise ValueError("structural signature spans multiple issue families")
        self._exact = exact
        self._structural = structural

    def reissue_owner(
        self,
        fingerprint: IssueFingerprint,
        *,
        issue_id: str,
    ) -> IssueFamilyDecision:
        """Reissue the current owner credential after durable index replay."""

        if not fingerprint.verify():
            raise ValueError("family owner reissue requires an intact fingerprint")
        family_id = self._issue_families.get(issue_id)
        if (
            family_id is None
            or self._owners.get(family_id) != issue_id
            or self._members[family_id].get(issue_id) != fingerprint.exact
            or self._fingerprints.get(issue_id) != fingerprint
        ):
            raise ValueError("issue is not the current family owner")
        decision = IssueFamilyDecision._from_index(
            issue_id=issue_id,
            owner_issue_id=issue_id,
            family_id=family_id,
            fingerprint=fingerprint,
            match_kind=FamilyMatchKind.FRESH,
            matched_issue_id=None,
            known_regression_match=False,
            known_regression_case_id=None,
            is_new_family=True,
            family_members=tuple(sorted(self._members[family_id].items())),
            family_fingerprints=self._family_fingerprint_proof(family_id),
            token=_DECISION_TOKEN,
        )
        self._decisions[family_id].append(decision)
        return decision

    def reissue_member(
        self,
        fingerprint: IssueFingerprint,
        *,
        issue_id: str,
    ) -> IssueFamilyDecision:
        """Reissue a current non-owner credential for durable union migration."""

        if not fingerprint.verify():
            raise ValueError("family member reissue requires an intact fingerprint")
        family_id = self._issue_families.get(issue_id)
        if (
            family_id is None
            or self._owners.get(family_id) == issue_id
            or self._fingerprints.get(issue_id) != fingerprint
        ):
            raise ValueError("issue is not a current non-owner family member")
        exact_match = _first_matching_issue(
            {
                member: exact
                for member, exact in self._members[family_id].items()
                if member != issue_id
            },
            fingerprint.exact,
        )
        match_kind = (
            FamilyMatchKind.EXACT
            if exact_match is not None
            else FamilyMatchKind.STRUCTURAL
        )
        decision = IssueFamilyDecision._from_index(
            issue_id=issue_id,
            owner_issue_id=self._owners[family_id],
            family_id=family_id,
            fingerprint=fingerprint,
            match_kind=match_kind,
            matched_issue_id=exact_match or self._owners[family_id],
            known_regression_match=False,
            known_regression_case_id=None,
            is_new_family=False,
            family_members=tuple(sorted(self._members[family_id].items())),
            family_fingerprints=self._family_fingerprint_proof(family_id),
            token=_DECISION_TOKEN,
        )
        self._decisions[family_id].append(decision)
        return decision


def fingerprint_issue(subject: FingerprintSubject) -> IssueFingerprint:
    if not isinstance(subject, FingerprintSubject):
        raise TypeError("fingerprint_issue requires FingerprintSubject")
    query_a_hash = _query_hash(subject.reduced_query_a)
    query_b_hash = _query_hash(subject.reduced_query_b)
    normalized_a = _normalize_parser_proven_query(subject.reduced_query_a)
    normalized_b = _normalize_parser_proven_query(subject.reduced_query_b)
    normalized_pair = (normalized_a, normalized_b) if normalized_a is not None and normalized_b is not None else None
    exact = _exact_signature(
        dbms=subject.dbms,
        dbms_version=subject.dbms_version,
        dbms_fingerprint=subject.dbms_fingerprint,
        canonical_rule_id=subject.canonical_rule_id,
        query_a_hash=query_a_hash,
        query_b_hash=query_b_hash,
        runtime_direction=subject.runtime_direction,
    )
    structural = _structural_signature(
        dbms=subject.dbms,
        dbms_version=subject.dbms_version,
        dbms_fingerprint=subject.dbms_fingerprint,
        canonical_rule_id=subject.canonical_rule_id,
        site_class=subject.site_class,
        normalized_pair=normalized_pair,
        runtime_direction=subject.runtime_direction,
    )
    values = {
        "dbms": subject.dbms,
        "dbms_version": subject.dbms_version,
        "dbms_fingerprint": subject.dbms_fingerprint,
        "graph_snapshot_hash": subject.graph_snapshot_hash,
        "canonical_rule_id": subject.canonical_rule_id,
        "reduced_query_a": subject.reduced_query_a,
        "reduced_query_b": subject.reduced_query_b,
        "query_a_hash": query_a_hash,
        "query_b_hash": query_b_hash,
        "runtime_direction": subject.runtime_direction,
        "site_class": subject.site_class,
        "submission_path": subject.submission_path,
        "reduced_validation_phenotype": subject.reduced_validation_phenotype,
        "exact": exact,
        "structural": structural,
        "normalized_pair": normalized_pair,
    }
    provisional = IssueFingerprint(**values, fingerprint_hash="")
    return IssueFingerprint(**values, fingerprint_hash=_fingerprint_hash(provisional, exact=exact, structural=structural))


def fingerprint_validated_reduction(
    validation: object,
    reduction: object,
    certificate: object,
    *,
    dbms: str,
) -> IssueFingerprint:
    """Derive the only promotion fingerprint from sealed validation evidence."""

    from graph_sym_perf.validation.models import IssueValidationReport
    from graph_sym_perf.validation.promotion import (
        validated_reduction_identity_is_consistent,
    )

    final_validation = getattr(reduction, "final_validation", None)
    if not isinstance(validation, IssueValidationReport) or not isinstance(
        final_validation, IssueValidationReport
    ):
        raise TypeError("family fingerprint requires original and reduced validation reports")
    if not validated_reduction_identity_is_consistent(
        validation,
        reduction,
        certificate,
    ):
        raise ValueError("family fingerprint requires one verified validation identity")
    target = validation.manifest.resolved_config.get("target")
    if not isinstance(target, str) or target != dbms:
        raise ValueError("family fingerprint DBMS does not match sealed target")
    if final_validation.submission_path is None:
        raise ValueError("family fingerprint requires a submission path")
    direction = getattr(getattr(reduction, "final_evidence", None), "slow_query", None)
    return fingerprint_issue(
        FingerprintSubject(
            dbms=dbms,
            dbms_version=validation.manifest.dbms_version,
            dbms_fingerprint=validation.manifest.pair.dbms_fingerprint,
            graph_snapshot_hash=validation.manifest.graph_snapshot.content_hash,
            canonical_rule_id=str(getattr(certificate, "rule_id")),
            reduced_query_a=str(getattr(reduction, "reduced_pair").query_a),
            reduced_query_b=str(getattr(reduction, "reduced_pair").query_b),
            runtime_direction=str(direction or ""),
            site_class=str(getattr(certificate, "site_class")),
            submission_path=final_validation.submission_path.value,
            reduced_validation_phenotype=(
                f"{final_validation.submission_path.value}:"
                f"{final_validation.magnitude_stability}:{direction}"
            ),
        )
    )


def known_regression_signature_from_manifest(manifest_path: str | Path) -> KnownRegressionSignature:
    from graph_sym_perf.validation.regression import RegressionManifest

    manifest = RegressionManifest.load(manifest_path)
    return KnownRegressionSignature._from_manifest(
        case_id=manifest.case_id,
        manifest_sha256=manifest.manifest_sha256,
        dbms=manifest.dbms,
        canonical_rule_id=manifest.canonical_rule_id,
        graph_snapshot_hash=manifest.graph_snapshot.content_hash,
        query_a_hash=_query_hash(manifest.fast_query),
        query_b_hash=_query_hash(manifest.slow_query),
        runtime_direction=manifest.expected_slow_query,
        token=_KNOWN_REGRESSION_TOKEN,
    )


def compact_family_decision_payload(decision: IssueFamilyDecision) -> dict[str, object]:
    """Serialize a family owner without copying every member's raw queries."""

    if not isinstance(decision, IssueFamilyDecision) or not decision.verify():
        raise ValueError("compact family proof requires a current family decision")
    return {
        "issue_id": decision.issue_id,
        "owner_issue_id": decision.owner_issue_id,
        "family_id": decision.family_id,
        "fingerprint": decision.fingerprint.__dict__,
        "match_kind": decision.match_kind.value,
        "matched_issue_id": decision.matched_issue_id,
        "known_regression_match": decision.known_regression_match,
        "known_regression_case_id": decision.known_regression_case_id,
        "is_new_family": decision.is_new_family,
        "member_count": decision.member_count,
        "member_proof_root": decision.member_proof_root,
        "registry_proof_ref": dict(_DURABLE_FAMILY_PROOF_REF),
        "family_version_hash": decision.family_version_hash,
        "decision_hash": decision.decision_hash,
    }


def family_member_proof_root(
    family_fingerprints: Iterable[tuple[str, IssueFingerprint]],
) -> str:
    """Bind compact descriptors while keeping each member's raw proof elsewhere."""

    descriptors: list[dict[str, object]] = []
    seen: set[str] = set()
    for issue_id, fingerprint in sorted(family_fingerprints, key=lambda item: item[0]):
        if (
            not isinstance(issue_id, str)
            or not issue_id.strip()
            or issue_id in seen
            or not isinstance(fingerprint, IssueFingerprint)
            or not fingerprint.verify()
        ):
            raise ValueError("family member proof is invalid or duplicated")
        seen.add(issue_id)
        descriptors.append(
            {
                "issue_id": issue_id,
                "exact": fingerprint.exact,
                "fingerprint_hash": fingerprint.fingerprint_hash,
                "canonical_seed": _new_family_id(fingerprint),
            }
        )
    if not descriptors:
        raise ValueError("family member proof cannot be empty")
    return _hash_payload("durable-family-member-root-v1", descriptors)


def compact_family_proof_is_valid(
    value: Mapping[str, object],
    family_fingerprints: Iterable[tuple[str, IssueFingerprint]],
) -> bool:
    """Recompute a compact artifact snapshot from raw member fingerprints."""

    try:
        proof = tuple(sorted(family_fingerprints, key=lambda item: item[0]))
        root = family_member_proof_root(proof)
        owner_issue_id = str(value["owner_issue_id"])
        family_id = str(value["family_id"])
        serialized_fingerprint = value["fingerprint"]
        if not isinstance(serialized_fingerprint, Mapping):
            return False
        fingerprint = next(
            member_fingerprint
            for member, member_fingerprint in proof
            if member == owner_issue_id
        )
        expected_owner = min(
            proof,
            key=lambda item: (_new_family_id(item[1]), item[0]),
        )[0]
    except (KeyError, StopIteration, TypeError, ValueError):
        return False
    return bool(
        len(proof) == value.get("member_count")
        and root == value.get("member_proof_root")
        and expected_owner == owner_issue_id
        and family_id == min(_new_family_id(item[1]) for item in proof)
        and fingerprint.verify()
        and fingerprint.fingerprint_hash
        == serialized_fingerprint.get("fingerprint_hash")
    )


def serialized_family_decision_is_valid(value: object) -> bool:
    """Rebuild every content-bound family field from a sealed artifact payload."""

    if not isinstance(value, Mapping):
        return False
    if "family_members" not in value and "family_fingerprints" not in value:
        return _serialized_compact_family_decision_is_valid(value)
    fingerprint_value = value.get("fingerprint")
    if not isinstance(fingerprint_value, Mapping):
        return False
    try:
        fingerprint_fields = dict(fingerprint_value)
        normalized_pair = fingerprint_fields.get("normalized_pair")
        if normalized_pair is not None:
            fingerprint_fields["normalized_pair"] = tuple(normalized_pair)
        fingerprint = IssueFingerprint(**fingerprint_fields)
        family_members = tuple(
            (str(member[0]), str(member[1])) for member in value.get("family_members", ())
        )
        rebuilt_fingerprints: list[tuple[str, IssueFingerprint]] = []
        for member in value.get("family_fingerprints", ()):
            member_value = dict(member[1])
            member_normalized_pair = member_value.get("normalized_pair")
            if member_normalized_pair is not None:
                member_value["normalized_pair"] = tuple(member_normalized_pair)
            rebuilt_fingerprints.append(
                (str(member[0]), IssueFingerprint(**member_value))
            )
        family_fingerprints = tuple(rebuilt_fingerprints)
        match_kind = FamilyMatchKind(str(value.get("match_kind")))
        family_id = str(value.get("family_id"))
        issue_id = str(value.get("issue_id"))
        expected = IssueFamilyDecision._from_index(
            issue_id=issue_id,
            owner_issue_id=value.get("owner_issue_id"),
            family_id=family_id,
            fingerprint=fingerprint,
            match_kind=match_kind,
            matched_issue_id=value.get("matched_issue_id"),
            known_regression_match=bool(value.get("known_regression_match")),
            known_regression_case_id=value.get("known_regression_case_id"),
            is_new_family=bool(value.get("is_new_family")),
            family_members=family_members,
            family_fingerprints=family_fingerprints,
            token=_DECISION_TOKEN,
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return False
    return bool(
        fingerprint.verify()
        and match_kind is FamilyMatchKind.FRESH
        and value.get("matched_issue_id") is None
        and value.get("owner_issue_id") == issue_id
        and value.get("known_regression_match") is False
        and value.get("known_regression_case_id") is None
        and value.get("is_new_family") is True
        and family_id == _new_family_id(fingerprint)
        and family_members == tuple(sorted(family_members))
        and family_fingerprints == tuple(sorted(family_fingerprints))
        and len({member for member, _exact in family_members}) == len(family_members)
        and len({member for member, _fingerprint in family_fingerprints})
        == len(family_fingerprints)
        and {member for member, _exact in family_members}
        == {member for member, _fingerprint in family_fingerprints}
        and all(
            dict(family_members).get(member) == member_fingerprint.exact
            for member, member_fingerprint in family_fingerprints
        )
        and (issue_id, fingerprint.exact) in family_members
        and (issue_id, fingerprint) in family_fingerprints
        and all(member_fingerprint.verify() for _member, member_fingerprint in family_fingerprints)
        and family_id
        == min(_new_family_id(member_fingerprint) for _member, member_fingerprint in family_fingerprints)
        and value.get("family_version_hash") == expected.family_version_hash
        and value.get("decision_hash") == expected.decision_hash
        and expected.verify()
    )


def _serialized_compact_family_decision_is_valid(value: Mapping[str, object]) -> bool:
    fingerprint_value = value.get("fingerprint")
    if not isinstance(fingerprint_value, Mapping):
        return False
    try:
        fingerprint_fields = dict(fingerprint_value)
        normalized_pair = fingerprint_fields.get("normalized_pair")
        if normalized_pair is not None:
            fingerprint_fields["normalized_pair"] = tuple(normalized_pair)
        fingerprint = IssueFingerprint(**fingerprint_fields)
        issue_id = str(value["issue_id"])
        family_id = str(value["family_id"])
        member_count = value["member_count"]
        member_proof_root = str(value["member_proof_root"])
        proof_ref = value["registry_proof_ref"]
        family_version_hash = _compact_family_version_hash(
            family_id,
            member_count,
            member_proof_root,
            None,
        )
        fields = {
            "issue_id": issue_id,
            "owner_issue_id": value.get("owner_issue_id"),
            "family_id": family_id,
            "fingerprint_hash": fingerprint.fingerprint_hash,
            "match_kind": value.get("match_kind"),
            "matched_issue_id": value.get("matched_issue_id"),
            "known_regression_match": value.get("known_regression_match"),
            "known_regression_case_id": value.get("known_regression_case_id"),
            "is_new_family": value.get("is_new_family"),
            "member_count": member_count,
            "member_proof_root": member_proof_root,
            "family_version_hash": family_version_hash,
        }
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        fingerprint.verify()
        and isinstance(member_count, int)
        and not isinstance(member_count, bool)
        and member_count >= 1
        and re.fullmatch(r"[0-9a-f]{64}", member_proof_root) is not None
        and isinstance(proof_ref, Mapping)
        and dict(proof_ref) == _DURABLE_FAMILY_PROOF_REF
        and value.get("owner_issue_id") == issue_id
        and value.get("match_kind") == FamilyMatchKind.FRESH.value
        and value.get("matched_issue_id") is None
        and value.get("known_regression_match") is False
        and value.get("known_regression_case_id") is None
        and value.get("is_new_family") is True
        and family_id == _new_family_id(fingerprint)
        and value.get("family_version_hash") == family_version_hash
        and value.get("decision_hash")
        == _hash_payload("family-decision-v1", fields)
    )


def _normalize_parser_proven_query(query: str) -> str | None:
    """Normalize only when every byte is covered by a supported project AST."""

    try:
        dependency = analyze_query(query)
        parsed = parse_query(query)
        if not dependency.valid or not parsed.clauses:
            return None
        if query[: parsed.clauses[0].start].strip() or query[parsed.clauses[-1].end :].strip():
            return None
        if any(
            query[left.end : right.start].strip()
            for left, right in zip(parsed.clauses, parsed.clauses[1:], strict=False)
        ):
            return None
        if tuple(clause.keyword for clause in parsed.clauses[:-1]) != ("MATCH",) * (len(parsed.clauses) - 1):
            return None
        if parsed.clauses[-1].keyword != "RETURN":
            return None
        matches = tuple(parse_match_clause(clause) for clause in parsed.clauses[:-1])
        if any(match.where_region is not None for match in matches):
            return None
        if any(not _pattern_relationships_are_fully_covered(match.pattern) for match in matches):
            return None
        if not _simple_return_is_fully_covered(parsed.clauses[-1].text, dependency):
            return None
    except (CypherParseError, ValueError, TypeError):
        return None

    variables: set[str] = set()
    for match in matches:
        variables.update(_pattern_variables(match.pattern))
    roles = _RoleMaps()
    tokens = _lex(query)
    rendered: list[str] = []
    square_depth = 0
    brace_depth = 0
    for index, token in enumerate(tokens):
        previous = tokens[index - 1].text if index else ""
        following = tokens[index + 1].text if index + 1 < len(tokens) else ""
        if token.text == "[":
            square_depth += 1
        elif token.text == "]":
            square_depth = max(0, square_depth - 1)
        elif token.text == "{":
            brace_depth += 1
        elif token.text == "}":
            brace_depth = max(0, brace_depth - 1)
        if token.kind == "string":
            rendered.append(roles.literal(token.text, "STRING"))
        elif token.kind == "number":
            rendered.append(roles.literal(token.text, "FLOAT" if "." in token.text else "INT"))
        elif token.kind == "identifier":
            upper = token.text.upper()
            if upper in {"TRUE", "FALSE", "NULL"}:
                rendered.append(roles.literal(upper, "BOOL" if upper != "NULL" else "NULL"))
            elif brace_depth and following == ":":
                rendered.append(roles.property_name(token.text))
            elif previous == ":" and not brace_depth:
                rendered.append(roles.relationship_type(token.text) if square_depth else roles.label(token.text))
            elif previous == ".":
                rendered.append(roles.property_name(token.text))
            elif token.text in variables:
                rendered.append(roles.variable(token.text))
            else:
                rendered.append(upper)
        else:
            rendered.append(token.text)
    return " ".join(rendered)


def _pattern_relationships_are_fully_covered(
    pattern: NodePattern | EdgePattern | VarLengthPathPattern | PatternList,
) -> bool:
    """Reject relationship text unless a total, deliberately small grammar covers it."""

    if isinstance(pattern, NodePattern):
        return True
    if isinstance(pattern, (EdgePattern, VarLengthPathPattern)):
        return _relationship_is_fully_covered(pattern.relationship)
    if isinstance(pattern, PatternList):
        return all(_pattern_relationships_are_fully_covered(atom) for atom in pattern.atoms)
    return False


def _relationship_is_fully_covered(relationship: object) -> bool:
    raw = getattr(relationship, "text", None)
    if not isinstance(raw, str):
        return False
    covered = _SUPPORTED_RELATIONSHIP_RE.fullmatch(raw)
    if covered is None:
        return False
    parsed_types = tuple(re.findall(r":\s*([A-Za-z]\w*)", covered.group("types") or ""))
    has_length = covered.group("length") is not None
    has_properties = covered.group("properties") is not None
    return bool(
        covered.group("variable") == getattr(relationship, "variable", None)
        and parsed_types == tuple(getattr(relationship, "types", ()))
        and has_length == (getattr(relationship, "var_length", None) is not None)
        and has_properties == ("{" in raw)
    )


def _simple_return_is_fully_covered(text: str, dependency: object) -> bool:
    body = text[len("RETURN") :].strip()
    if not body:
        return False
    bound = frozenset().union(*(clause.outgoing_scope for clause in dependency.clauses[:-1]))
    for item in split_top_level_commas(body):
        tokens = _lex(item.strip())
        if len(tokens) == 1 and tokens[0].kind == "identifier" and tokens[0].text in bound:
            continue
        if (
            len(tokens) == 3
            and tokens[0].kind == "identifier"
            and tokens[0].text in bound
            and tokens[1].text == "."
            and tokens[2].kind == "identifier"
        ):
            continue
        return False
    return True


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str


def _lex(text: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            end = index + 1
            while end < len(text):
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == quote:
                    end += 1
                    break
                end += 1
            if end > len(text) or not text[index:end].endswith(quote):
                raise ValueError("unterminated string")
            tokens.append(_Token("string", text[index:end]))
            index = end
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(text) and (text[end].isalnum() or text[end] == "_"):
                end += 1
            tokens.append(_Token("identifier", text[index:end]))
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < len(text) and (text[end].isdigit() or text[end] == "."):
                end += 1
            tokens.append(_Token("number", text[index:end]))
            index = end
            continue
        if text[index : index + 2] in {"->", "<-", ">=", "<=", "<>", "!=", "=~"}:
            tokens.append(_Token("symbol", text[index : index + 2]))
            index += 2
            continue
        tokens.append(_Token("symbol", char))
        index += 1
    return tuple(tokens)


class _RoleMaps:
    def __init__(self) -> None:
        self._roles: dict[str, dict[str, str]] = {
            "VARIABLE": {},
            "LABEL": {},
            "RELTYPE": {},
            "PROPERTY": {},
            "LITERAL_STRING": {},
            "LITERAL_INT": {},
            "LITERAL_FLOAT": {},
            "LITERAL_BOOL": {},
            "LITERAL_NULL": {},
        }

    def _value(self, role: str, value: str) -> str:
        values = self._roles[role]
        return values.setdefault(value, f"<{role}_{len(values)}>")

    def variable(self, value: str) -> str:
        return self._value("VARIABLE", value)

    def label(self, value: str) -> str:
        return self._value("LABEL", value)

    def relationship_type(self, value: str) -> str:
        return self._value("RELTYPE", value)

    def property_name(self, value: str) -> str:
        return self._value("PROPERTY", value)

    def literal(self, value: str, kind: str) -> str:
        return self._value(f"LITERAL_{kind}", value)


def _pattern_variables(pattern: object) -> frozenset[str]:
    if isinstance(pattern, PatternList):
        return frozenset().union(*(_pattern_variables(atom) for atom in pattern.atoms))
    if isinstance(pattern, NodePattern):
        return frozenset({pattern.variable})
    if isinstance(pattern, EdgePattern):
        values = {pattern.left.variable, pattern.right.variable}
        if pattern.relationship.variable:
            values.add(pattern.relationship.variable)
        return frozenset(values)
    if isinstance(pattern, VarLengthPathPattern):
        values = {pattern.left.variable, pattern.right.variable}
        if pattern.relationship.variable:
            values.add(pattern.relationship.variable)
        if pattern.path_variable:
            values.add(pattern.path_variable)
        return frozenset(values)
    return frozenset()


def _exact_signature(**payload: object) -> str:
    return _hash_payload(f"{_FINGERPRINT_VERSION}:exact", payload)


def _structural_signature(
    *,
    dbms: str,
    dbms_version: str,
    dbms_fingerprint: str,
    canonical_rule_id: str,
    site_class: str,
    normalized_pair: tuple[str, str] | None,
    runtime_direction: str,
) -> str | None:
    if normalized_pair is None:
        return None
    return _hash_payload(
        f"{_FINGERPRINT_VERSION}:structural",
        {
            "dbms": dbms,
            "dbms_version": dbms_version,
            "dbms_fingerprint": dbms_fingerprint,
            "canonical_rule_id": canonical_rule_id,
            "site_class": site_class,
            "normalized_pair": normalized_pair,
            "runtime_direction": runtime_direction,
        },
    )


def _fingerprint_hash(fingerprint: IssueFingerprint, *, exact: str, structural: str | None) -> str:
    return _hash_payload(
        _FINGERPRINT_VERSION,
        {
            "dbms": fingerprint.dbms,
            "dbms_version": fingerprint.dbms_version,
            "dbms_fingerprint": fingerprint.dbms_fingerprint,
            "graph_snapshot_hash": fingerprint.graph_snapshot_hash,
            "canonical_rule_id": fingerprint.canonical_rule_id,
            "reduced_query_a": fingerprint.reduced_query_a,
            "reduced_query_b": fingerprint.reduced_query_b,
            "query_a_hash": fingerprint.query_a_hash,
            "query_b_hash": fingerprint.query_b_hash,
            "runtime_direction": fingerprint.runtime_direction,
            "site_class": fingerprint.site_class,
            "submission_path": fingerprint.submission_path,
            "reduced_validation_phenotype": fingerprint.reduced_validation_phenotype,
            "exact": exact,
            "structural": structural,
            "normalized_pair": fingerprint.normalized_pair,
        },
    )


def _known_signature_hash(signature: KnownRegressionSignature) -> str:
    return _hash_payload(
        "known-regression-signature-v1",
        {
            "case_id": signature.case_id,
            "manifest_sha256": signature.manifest_sha256,
            "dbms": signature.dbms,
            "canonical_rule_id": signature.canonical_rule_id,
            "graph_snapshot_hash": signature.graph_snapshot_hash,
            "query_a_hash": signature.query_a_hash,
            "query_b_hash": signature.query_b_hash,
            "runtime_direction": signature.runtime_direction,
        },
    )


def _structural_family_key(fingerprint: IssueFingerprint) -> tuple[str, str, str] | None:
    if fingerprint.structural is None:
        return None
    return (
        fingerprint.structural,
        fingerprint.submission_path,
        fingerprint.reduced_validation_phenotype,
    )


def _new_family_id(fingerprint: IssueFingerprint) -> str:
    structural_key = _structural_family_key(fingerprint)
    payload: object = structural_key if structural_key is not None else fingerprint.exact
    return _hash_payload(_FAMILY_VERSION, payload)


def _family_version_hash(
    family_id: str | None,
    members: tuple[tuple[str, str], ...],
    fingerprints: tuple[tuple[str, IssueFingerprint], ...],
    known_case_id: str | None,
) -> str:
    return _compact_family_version_hash(
        family_id,
        len(fingerprints),
        family_member_proof_root(fingerprints),
        known_case_id,
    )


def _compact_family_version_hash(
    family_id: str | None,
    member_count: int,
    member_proof_root: str,
    known_case_id: str | None,
) -> str:
    return _hash_payload(
        "issue-family-members-v2",
        {
            "family_id": family_id,
            "member_count": member_count,
            "member_proof_root": member_proof_root,
            "known_regression_case_id": known_case_id,
        },
    )


def _first_matching_issue(members: Mapping[str, str], exact: str) -> str | None:
    matches = tuple(sorted(issue_id for issue_id, signature in members.items() if signature == exact))
    return matches[0] if matches else None


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _hash_payload(domain: str, payload: object) -> str:
    return hashlib.sha256((domain + "\0" + stable_json_dumps(payload)).encode("utf-8")).hexdigest()


__all__ = [
    "FamilyMatchKind",
    "FingerprintSubject",
    "IssueFamilyDecision",
    "IssueFamilyIndex",
    "IssueFingerprint",
    "KnownRegressionSignature",
    "fingerprint_issue",
    "fingerprint_validated_reduction",
    "compact_family_decision_payload",
    "compact_family_proof_is_valid",
    "family_member_proof_root",
    "known_regression_signature_from_manifest",
    "serialized_family_decision_is_valid",
]
