"""Isolated known-regression gate for the frozen Neo4j BUG001 case.

Known regressions are blocking controls.  They reuse the independent Task-8
validation protocol, but they never enter discovery, promotion, or validated
runtime-yield accounting.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

import yaml

from graph_sym_perf.adapters.factory import create_adapter
from graph_sym_perf.core import (
    ConfirmationConfig,
    DatasetConfig,
    EquivalenceConfig,
    GdsmithConfig,
    QueryGenerationConfig,
    RunConfig,
    RunPaths,
    SystemConfig,
    TargetSystem,
    TransformConfig,
)
from graph_sym_perf.gdsmith.replay import parse_gdsmith_replay_graph
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import SafetyCertificate
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import (
    CandidateBundle,
    IssueValidationReport,
    PipelineStage,
    ValidationPolicy,
    canonical_content_hash,
)
from graph_sym_perf.validation.session_runner import SpawnedValidationSessionRunner
from graph_sym_perf.validation.validator import IssueValidator


_IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_AFFECTED_VERSION_RE = re.compile(r"(?P<version>\d{4}\.\d{2}\.\d+) community\Z")
_FORBIDDEN_QUERY_PREFIX_RE = re.compile(r"\b(?:PROFILE|EXPLAIN)\b", re.IGNORECASE)
_INDEX_STATEMENT_RE = re.compile(r"CREATE\s+INDEX\b", re.IGNORECASE)
_DEDICATED_ENV_PREFIX = "GRAPHSYMPERF_NEO4J_BUG001_"
_CURRENT_VERSION_STATUSES = frozenset(
    {"not_checked", "reproduced", "not_reproduced", "inconclusive"}
)


class RegressionConfigurationError(ValueError):
    """The frozen regression case cannot be resolved safely."""


class RegressionVersionMismatch(RuntimeError):
    def __init__(self, expected: str, observed: str) -> None:
        super().__init__(f"affected version mismatch: expected {expected}, observed {observed}")
        self.expected = expected
        self.observed = observed


class RegressionFailureKind(str, Enum):
    CONFIGURATION = "configuration"
    VERSION = "version"
    GRAPH = "graph"
    EQUIVALENCE = "equivalence"
    RUNTIME = "runtime"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class RegressionCase:
    schema_version: int
    case_id: str
    dbms: str
    affected_version: str
    image_ref: str
    image_digest: str
    uri: str
    user: str
    password: str = field(repr=False)
    graph_path: Path
    fast_query_path: Path
    slow_query_path: Path
    canonical_rule_id: str
    expected_graph_nodes: int
    expected_graph_relationships: int
    actual_graph_nodes: int
    actual_graph_relationships: int
    graph_file_sha256: str
    graph_snapshot: GraphSnapshot
    index_statements: tuple[str, ...]
    fast_query: str
    slow_query: str
    manifest_path: Path
    manifest_sha256: str


@dataclass(frozen=True)
class RegressionEnvironmentIdentity:
    observed_version: str
    dbms_fingerprint: str
    driver_version: str


@dataclass(frozen=True)
class RegressionGateReport:
    schema_version: int
    case_id: str
    known_regression: bool
    passed: bool
    affected_version_reproduced: bool
    failure_kind: RegressionFailureKind | None
    reason_codes: tuple[str, ...]
    affected_version: str
    observed_version: str | None
    current_version_status: str
    validation_stage: str | None
    validation_protocol: str
    validated_ratio: float | None
    pooled_delta_ms: float | None
    dbms_fingerprint: str | None
    driver_version: str | None
    image_ref: str
    image_digest: str
    uri_identity: str
    user_identity: str
    environment_identity_hash: str
    manifest_sha256: str
    graph_file_sha256: str
    graph_snapshot_sha256: str
    fast_query_sha256: str
    slow_query_sha256: str
    fresh_issue_count: int
    discovery_accepted_pairs: int
    validated_runtime_yield: int
    discovery_budget: int
    control_budget: int
    submit_ready_created: bool
    content_hash: str

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["failure_kind"] = (
            self.failure_kind.value if self.failure_kind is not None else None
        )
        payload["reason_codes"] = list(self.reason_codes)
        return payload


class _IssueValidatorLike(Protocol):
    def validate(
        self,
        candidate: CandidateBundle,
        session_runner: Any,
    ) -> IssueValidationReport: ...


def load_regression_case(
    manifest_path: str | Path,
    environment: Mapping[str, str] | None = None,
) -> RegressionCase:
    """Resolve the self-contained fixture and dedicated environment fail-closed."""

    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise RegressionConfigurationError(f"regression manifest not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RegressionConfigurationError("regression manifest is not readable YAML") from exc
    if not isinstance(raw, Mapping):
        raise RegressionConfigurationError("regression manifest root must be a mapping")
    schema_version = _required_int(raw, "schema_version")
    if schema_version != 1:
        raise RegressionConfigurationError("regression manifest schema_version must be 1")
    case_id = _required_text(raw, "case_id")
    dbms = _required_text(raw, "dbms").lower()
    if dbms != "neo4j":
        raise RegressionConfigurationError("BUG001 regression dbms must be neo4j")
    affected_version = _required_text(raw, "affected_version")
    version_match = _AFFECTED_VERSION_RE.fullmatch(affected_version)
    if version_match is None:
        raise RegressionConfigurationError(
            "affected_version must be an exact '<version> community' identity"
        )
    image_ref = _required_text(raw, "image_ref")
    expected_image_ref = f"neo4j:{version_match.group('version')}-community"
    if image_ref != expected_image_ref:
        raise RegressionConfigurationError(
            f"image_ref must freeze the affected version as {expected_image_ref}"
        )

    env = os.environ if environment is None else environment
    env_names = {
        field_name: _required_text(raw, field_name)
        for field_name in ("image_digest_env", "uri_env", "user_env", "password_env")
    }
    for field_name, env_name in env_names.items():
        if not env_name.startswith(_DEDICATED_ENV_PREFIX):
            raise RegressionConfigurationError(
                f"{field_name} must use the dedicated BUG001 environment namespace"
            )
    resolved_env = {
        field_name: _required_environment(env, env_name)
        for field_name, env_name in env_names.items()
    }
    image_digest = resolved_env["image_digest_env"]
    if _IMAGE_DIGEST_RE.fullmatch(image_digest) is None:
        raise RegressionConfigurationError(
            f"{env_names['image_digest_env']} must match sha256:<64 lowercase hex>"
        )
    uri = _validated_uri(resolved_env["uri_env"], env_names["uri_env"])

    graph_path = _local_fixture_path(path, raw, "graph_path")
    fast_query_path = _local_fixture_path(path, raw, "fast_query_path")
    slow_query_path = _local_fixture_path(path, raw, "slow_query_path")
    graph_bytes = _read_fixture_bytes(graph_path, "graph_path")
    graph_file_sha256 = hashlib.sha256(graph_bytes).hexdigest()
    expected_graph_sha256 = _required_text(raw, "graph_sha256")
    if _SHA256_RE.fullmatch(expected_graph_sha256) is None:
        raise RegressionConfigurationError("graph_sha256 must be 64 lowercase hex characters")
    if graph_file_sha256 != expected_graph_sha256:
        raise RegressionConfigurationError("graph fixture SHA-256 does not match manifest")
    try:
        graph_text = graph_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegressionConfigurationError("graph fixture must be UTF-8") from exc
    all_statements = tuple(line.strip() for line in graph_text.splitlines() if line.strip())
    index_statements = tuple(
        statement for statement in all_statements if _INDEX_STATEMENT_RE.match(statement)
    )
    graph_statements = tuple(
        statement for statement in all_statements if not _INDEX_STATEMENT_RE.match(statement)
    )
    try:
        parsed_graph = parse_gdsmith_replay_graph(graph_statements)
    except (TypeError, ValueError) as exc:
        raise RegressionConfigurationError("graph fixture is not a replayable GDsmith graph") from exc
    fast_query = _read_query_fixture(fast_query_path, "fast_query_path")
    slow_query = _read_query_fixture(slow_query_path, "slow_query_path")
    if fast_query == slow_query:
        raise RegressionConfigurationError("fast and slow regression queries must differ")

    canonical_rule_id = _required_text(raw, "canonical_rule_id")
    try:
        canonical_rule_id = RuleRegistry.default().resolve_id(canonical_rule_id)
    except KeyError as exc:
        raise RegressionConfigurationError(
            f"unknown canonical_rule_id: {canonical_rule_id}"
        ) from exc
    if canonical_rule_id != "pattern_order_symmetry":
        raise RegressionConfigurationError(
            "Neo4j BUG001 must remain bound to pattern_order_symmetry"
        )

    return RegressionCase(
        schema_version=schema_version,
        case_id=case_id,
        dbms=dbms,
        affected_version=affected_version,
        image_ref=image_ref,
        image_digest=image_digest,
        uri=uri,
        user=resolved_env["user_env"],
        password=resolved_env["password_env"],
        graph_path=graph_path,
        fast_query_path=fast_query_path,
        slow_query_path=slow_query_path,
        canonical_rule_id=canonical_rule_id,
        expected_graph_nodes=_required_positive_int(raw, "expected_graph_nodes"),
        expected_graph_relationships=_required_positive_int(
            raw, "expected_graph_relationships"
        ),
        actual_graph_nodes=len(parsed_graph.nodes),
        actual_graph_relationships=len(parsed_graph.relationships),
        graph_file_sha256=graph_file_sha256,
        graph_snapshot=GraphSnapshot.from_statements(graph_statements),
        index_statements=index_statements,
        fast_query=fast_query,
        slow_query=slow_query,
        manifest_path=path,
        manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


@dataclass(frozen=True)
class RegressionNeo4jAdapterFactory:
    """Pickle-safe adapter factory used by both independent validation sessions."""

    case: RegressionCase
    adapter_creator: Callable[..., Any] = create_adapter

    def __call__(self, _candidate: CandidateBundle | None) -> Any:
        system = SystemConfig(
            target=TargetSystem.NEO4J,
            required_env=(
                "GRAPHSYMPERF_NEO4J_URI",
                "GRAPHSYMPERF_NEO4J_USER",
                "GRAPHSYMPERF_NEO4J_PASSWORD",
            ),
            optional_env={},
            settings={
                "known_regression_case_id": self.case.case_id,
                "image_ref": self.case.image_ref,
                "image_digest": self.case.image_digest,
            },
        )
        run_config = RunConfig(
            source_path=str(self.case.manifest_path),
            paths=RunPaths(),
            systems=(system,),
            dataset=DatasetConfig(seed=0),
            query_generation=QueryGenerationConfig(
                max_cases=0,
                source="known_regression_fixture",
                gdsmith=GdsmithConfig(num_queries=0, random_seed=0, max_node_num=1),
            ),
            equivalence=EquivalenceConfig(max_result_rows=100_000, timeout_seconds=60.0),
            confirmation=ConfirmationConfig(timeout_seconds=60.0),
            transforms=TransformConfig(
                enabled_families=("symmetry",),
                enabled_rules=(self.case.canonical_rule_id,),
                max_variants_per_case=1,
            ),
        )
        environment = {
            "GRAPHSYMPERF_NEO4J_URI": self.case.uri,
            "GRAPHSYMPERF_NEO4J_USER": self.case.user,
            "GRAPHSYMPERF_NEO4J_PASSWORD": self.case.password,
        }
        delegate = self.adapter_creator(
            system,
            run_config,
            environment,
            allow_create=True,
        )
        return _AffectedVersionNeo4jAdapter(delegate, self.case)


class _AffectedVersionNeo4jAdapter:
    """Enforce version and fixed-schema loading inside every fresh session."""

    def __init__(self, delegate: Any, case: RegressionCase) -> None:
        self._delegate = delegate
        self._case = case
        self.observed_version: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def connectivity_check(self) -> tuple[bool, str]:
        connected = self._delegate.connectivity_check()
        if not isinstance(connected, tuple) or not connected or connected[0] is not True:
            return connected
        result = self._delegate.execute(
            "CALL dbms.components() YIELD versions, edition "
            "RETURN versions[0] AS version, edition",
            {},
            "regression_version_identity",
            max_rows=1,
            timeout_seconds=60.0,
        )
        rows = getattr(result, "rows", ())
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise RuntimeError("regression_version_identity_unavailable")
        version = rows[0].get("version")
        edition = rows[0].get("edition")
        if not isinstance(version, str) or not isinstance(edition, str):
            raise RuntimeError("regression_version_identity_unavailable")
        observed = f"{version.strip()} {edition.strip().lower()}"
        self.observed_version = observed
        if observed != self._case.affected_version:
            raise RegressionVersionMismatch(self._case.affected_version, observed)
        return connected

    def reset_graph(self, *, timeout_seconds: float | None = None) -> bool:
        reset_schema = getattr(self._delegate, "reset_schema", None)
        if callable(reset_schema):
            reset_schema()
        return bool(self._delegate.reset_graph(timeout_seconds=timeout_seconds))

    def load_gdsmith_graph(self, statements: tuple[str, ...] | list[str]) -> Any:
        for statement in self._case.index_statements:
            self._delegate.execute(
                statement,
                {},
                "regression_graph_index",
                timeout_seconds=60.0,
            )
        return self._delegate.load_gdsmith_graph(statements)


def build_independent_session_runner(case: RegressionCase) -> SpawnedValidationSessionRunner:
    """Production default: Task-8 fresh-process validation, version locked per session."""

    return SpawnedValidationSessionRunner(RegressionNeo4jAdapterFactory(case))


def _probe_environment_identity(case: RegressionCase) -> RegressionEnvironmentIdentity:
    adapter = RegressionNeo4jAdapterFactory(case)(None)
    try:
        connected = adapter.connectivity_check()
        if not isinstance(connected, tuple) or not connected or connected[0] is not True:
            raise RuntimeError("regression connectivity check failed")
        fingerprint = adapter.dbms_fingerprint()
        if not getattr(fingerprint, "available", False):
            raise RuntimeError("regression DBMS fingerprint unavailable")
        observed_version = adapter.observed_version
        if not observed_version:
            raise RuntimeError("regression version identity unavailable")
        driver_version = getattr(fingerprint, "driver_version", None)
        if not isinstance(driver_version, str) or not driver_version:
            raise RuntimeError("regression driver version unavailable")
        return RegressionEnvironmentIdentity(
            observed_version=observed_version,
            dbms_fingerprint=str(fingerprint.value),
            driver_version=driver_version,
        )
    finally:
        adapter.close()


class RegressionRunner:
    """Run BUG001 only as a blocking regression control."""

    def __init__(
        self,
        validator: _IssueValidatorLike | None = None,
        *,
        report_path: str | Path = "regression_report.json",
        identity_probe: Callable[[RegressionCase], RegressionEnvironmentIdentity] | None = None,
        session_runner_factory: Callable[[RegressionCase], Any] | None = None,
    ) -> None:
        self._validator = validator or IssueValidator()
        self.report_path = Path(report_path)
        self._identity_probe = identity_probe or _probe_environment_identity
        self._session_runner_factory = (
            session_runner_factory or build_independent_session_runner
        )

    def run(
        self,
        case: RegressionCase,
        *,
        current_version_status: str = "not_checked",
    ) -> RegressionGateReport:
        if not isinstance(case, RegressionCase):
            raise TypeError("RegressionRunner requires a RegressionCase")
        if current_version_status not in _CURRENT_VERSION_STATUSES:
            raise RegressionConfigurationError(
                "current_version_status must be not_checked, reproduced, not_reproduced, or inconclusive"
            )
        graph_reasons = _static_graph_reasons(case)
        if graph_reasons:
            return self._persist(
                _make_report(
                    case,
                    passed=False,
                    failure_kind=RegressionFailureKind.GRAPH,
                    reason_codes=graph_reasons,
                    current_version_status=current_version_status,
                )
            )

        try:
            identity = self._identity_probe(case)
        except RegressionVersionMismatch as exc:
            return self._persist(
                _make_report(
                    case,
                    passed=False,
                    failure_kind=RegressionFailureKind.VERSION,
                    reason_codes=("affected_version_mismatch",),
                    observed_version=exc.observed,
                    current_version_status=current_version_status,
                )
            )
        except Exception as exc:
            return self._persist(
                _make_report(
                    case,
                    passed=False,
                    failure_kind=RegressionFailureKind.INFRASTRUCTURE,
                    reason_codes=(f"identity_infrastructure_error:{type(exc).__name__}",),
                    current_version_status=current_version_status,
                )
            )
        if identity.observed_version != case.affected_version:
            return self._persist(
                _make_report(
                    case,
                    passed=False,
                    failure_kind=RegressionFailureKind.VERSION,
                    reason_codes=("affected_version_mismatch",),
                    observed_version=identity.observed_version,
                    identity=identity,
                    current_version_status=current_version_status,
                )
            )
        if (
            _SHA256_RE.fullmatch(identity.dbms_fingerprint) is None
            or not identity.driver_version.strip()
        ):
            return self._persist(
                _make_report(
                    case,
                    passed=False,
                    failure_kind=RegressionFailureKind.INFRASTRUCTURE,
                    reason_codes=("invalid_environment_identity",),
                    observed_version=identity.observed_version,
                    identity=identity,
                    current_version_status=current_version_status,
                )
            )

        candidate = _candidate(case, identity)
        try:
            validation = self._validator.validate(
                candidate,
                self._session_runner_factory(case),
            )
        except Exception as exc:
            return self._persist(
                _make_report(
                    case,
                    passed=False,
                    failure_kind=RegressionFailureKind.INFRASTRUCTURE,
                    reason_codes=(
                        f"validation_infrastructure_error:{type(exc).__name__}",
                    ),
                    observed_version=identity.observed_version,
                    identity=identity,
                    current_version_status=current_version_status,
                )
            )

        stage = getattr(validation, "stage", None)
        passed = stage is PipelineStage.VALIDATION_PASSED
        reasons = tuple(getattr(validation, "reason_codes", ()))
        failure_kind = None if passed else _validation_failure_kind(validation)
        if not passed and not reasons:
            reasons = ("known_regression_not_reproduced",)
        return self._persist(
            _make_report(
                case,
                passed=passed,
                failure_kind=failure_kind,
                reason_codes=reasons,
                observed_version=identity.observed_version,
                identity=identity,
                current_version_status=current_version_status,
                validation_stage=(stage.value if isinstance(stage, PipelineStage) else str(stage)),
                validation_protocol=str(
                    getattr(validation, "validation_protocol", "independent-two-session-v1")
                ),
                validated_ratio=getattr(validation, "validated_ratio", None),
                pooled_delta_ms=getattr(validation, "pooled_delta_ms", None),
            )
        )

    def _persist(self, report: RegressionGateReport) -> RegressionGateReport:
        write_regression_report(report, self.report_path)
        return report


def _candidate(
    case: RegressionCase,
    identity: RegressionEnvironmentIdentity,
) -> CandidateBundle:
    pair = EquivalentQueryPair(
        query_a=case.fast_query,
        query_b=case.slow_query,
        parameters={},
        graph_snapshot_hash=case.graph_snapshot.content_hash,
        dbms_fingerprint=identity.dbms_fingerprint,
    )
    rule = RuleRegistry.default().get(case.canonical_rule_id)
    certificate = SafetyCertificate(
        rule.rule_id,
        "clauses[4].pattern.components[0]",
        (),
        "graph_pattern",
        "pattern_defined",
        "match",
        rule.purity_requirements,
        rule.proof_id,
        "known-regression-neo4j-bug001",
    )
    return CandidateBundle.create(
        pair,
        graph_snapshot=case.graph_snapshot,
        dbms_version=case.affected_version,
        resolved_config={
            "target": case.dbms,
            "known_regression_case_id": case.case_id,
            "affected_version": case.affected_version,
            "image_ref": case.image_ref,
            "image_digest": case.image_digest,
            "uri_identity": _uri_identity(case.uri),
            "volatile_functions": (),
        },
        discovery_slow_query="b",
        discovery_ratio=3.0,
        certificate=certificate,
        validation_policy=ValidationPolicy(
            max_result_rows=100_000,
            query_timeout_seconds=60.0,
            session_timeout_seconds=600.0,
        ),
    )


def _static_graph_reasons(case: RegressionCase) -> tuple[str, ...]:
    reasons: list[str] = []
    if (
        case.actual_graph_nodes != case.expected_graph_nodes
        or case.actual_graph_relationships != case.expected_graph_relationships
    ):
        reasons.append("graph_count_mismatch")
    if hashlib.sha256(case.graph_path.read_bytes()).hexdigest() != case.graph_file_sha256:
        reasons.append("graph_file_hash_mismatch")
    return tuple(reasons)


def _validation_failure_kind(validation: IssueValidationReport) -> RegressionFailureKind:
    sessions = tuple(getattr(validation, "sessions", ()))
    session_reasons = tuple(
        str(reason)
        for session in sessions
        for reason in getattr(getattr(session, "evidence", None), "reason_codes", ())
    )
    if any("affected version mismatch" in reason for reason in session_reasons):
        return RegressionFailureKind.VERSION
    if any(
        getattr(session, "lifecycle", None) is not None
        and getattr(session.lifecycle, "graph_loaded", False)
        and not getattr(session.lifecycle, "graph_verified", False)
        for session in sessions
    ):
        return RegressionFailureKind.GRAPH
    if any(
        getattr(session, "lifecycle", None) is not None
        and not getattr(session.lifecycle, "complete", False)
        for session in sessions
    ):
        return RegressionFailureKind.INFRASTRUCTURE
    equivalence = getattr(validation, "equivalence", None)
    if equivalence is not None and (
        not getattr(equivalence, "equivalent", False)
        or not getattr(equivalence, "complete_result", False)
    ):
        return RegressionFailureKind.EQUIVALENCE
    reasons = tuple(str(reason) for reason in getattr(validation, "reason_codes", ()))
    if any(
        token in reason
        for reason in reasons
        for token in ("ratio", "delta", "runtime", "timeout", "direction", "noise")
    ):
        return RegressionFailureKind.RUNTIME
    if getattr(validation, "stage", None) in {
        PipelineStage.REJECTED,
        PipelineStage.INCONCLUSIVE,
    }:
        return RegressionFailureKind.RUNTIME
    return RegressionFailureKind.INFRASTRUCTURE


def _make_report(
    case: RegressionCase,
    *,
    passed: bool,
    failure_kind: RegressionFailureKind | None,
    reason_codes: tuple[str, ...],
    current_version_status: str,
    observed_version: str | None = None,
    identity: RegressionEnvironmentIdentity | None = None,
    validation_stage: str | None = None,
    validation_protocol: str = "independent-two-session-v1",
    validated_ratio: float | None = None,
    pooled_delta_ms: float | None = None,
) -> RegressionGateReport:
    uri_identity = _uri_identity(case.uri)
    environment_identity: dict[str, object] = {
        "affected_version": case.affected_version,
        "observed_version": observed_version,
        "image_ref": case.image_ref,
        "image_digest": case.image_digest,
        "uri_identity": uri_identity,
        "user_identity": case.user,
        "dbms_fingerprint": identity.dbms_fingerprint if identity else None,
        "driver_version": identity.driver_version if identity else None,
    }
    values: dict[str, Any] = {
        "schema_version": 1,
        "case_id": case.case_id,
        "known_regression": True,
        "passed": passed,
        "affected_version_reproduced": passed,
        "failure_kind": failure_kind,
        "reason_codes": reason_codes,
        "affected_version": case.affected_version,
        "observed_version": observed_version,
        "current_version_status": current_version_status,
        "validation_stage": validation_stage,
        "validation_protocol": validation_protocol,
        "validated_ratio": validated_ratio,
        "pooled_delta_ms": pooled_delta_ms,
        "dbms_fingerprint": identity.dbms_fingerprint if identity else None,
        "driver_version": identity.driver_version if identity else None,
        "image_ref": case.image_ref,
        "image_digest": case.image_digest,
        "uri_identity": uri_identity,
        "user_identity": case.user,
        "environment_identity_hash": canonical_content_hash(environment_identity),
        "manifest_sha256": case.manifest_sha256,
        "graph_file_sha256": case.graph_file_sha256,
        "graph_snapshot_sha256": case.graph_snapshot.content_hash,
        "fast_query_sha256": hashlib.sha256(case.fast_query.encode("utf-8")).hexdigest(),
        "slow_query_sha256": hashlib.sha256(case.slow_query.encode("utf-8")).hexdigest(),
        "fresh_issue_count": 0,
        "discovery_accepted_pairs": 0,
        "validated_runtime_yield": 0,
        "discovery_budget": 0,
        "control_budget": 0,
        "submit_ready_created": False,
    }
    hash_payload = {
        **values,
        "failure_kind": failure_kind.value if failure_kind is not None else None,
        "reason_codes": list(reason_codes),
    }
    return RegressionGateReport(
        **values,
        content_hash=canonical_content_hash(hash_payload),
    )


def write_regression_report(report: RegressionGateReport, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        report.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ) + "\n"
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory = os.open(
            destination.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _required_text(raw: Mapping[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RegressionConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def _required_int(raw: Mapping[str, object], name: str) -> int:
    value = raw.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RegressionConfigurationError(f"{name} must be an integer")
    return value


def _required_positive_int(raw: Mapping[str, object], name: str) -> int:
    value = _required_int(raw, name)
    if value <= 0:
        raise RegressionConfigurationError(f"{name} must be positive")
    return value


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RegressionConfigurationError(f"required regression environment is missing: {name}")
    return value.strip()


def _local_fixture_path(
    manifest_path: Path,
    raw: Mapping[str, object],
    field_name: str,
) -> Path:
    relative = Path(_required_text(raw, field_name))
    if relative.is_absolute():
        raise RegressionConfigurationError(f"{field_name} must be fixture-relative")
    resolved = (manifest_path.parent / relative).resolve()
    try:
        resolved.relative_to(manifest_path.parent.resolve())
    except ValueError as exc:
        raise RegressionConfigurationError(
            f"{field_name} must remain inside the regression fixture directory"
        ) from exc
    if not resolved.is_file():
        raise RegressionConfigurationError(f"{field_name} fixture not found: {resolved}")
    return resolved


def _read_fixture_bytes(path: Path, field_name: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RegressionConfigurationError(f"cannot read {field_name} fixture") from exc


def _read_query_fixture(path: Path, field_name: str) -> str:
    try:
        query = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise RegressionConfigurationError(f"cannot read {field_name} fixture") from exc
    if not query:
        raise RegressionConfigurationError(f"{field_name} query fixture is empty")
    if _FORBIDDEN_QUERY_PREFIX_RE.search(query):
        raise RegressionConfigurationError(
            f"{field_name} query fixture must not contain PROFILE or EXPLAIN"
        )
    return query


def _validated_uri(value: str, env_name: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RegressionConfigurationError(f"{env_name} is not a valid Neo4j URI") from exc
    if parsed.scheme.lower() not in {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}:
        raise RegressionConfigurationError(f"{env_name} must use a Neo4j driver URI scheme")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise RegressionConfigurationError(
            f"{env_name} must provide a host and must not embed credentials"
        )
    if parsed.query or parsed.fragment:
        raise RegressionConfigurationError(f"{env_name} must not contain query or fragment data")
    netloc = parsed.hostname
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _uri_identity(uri: str) -> str:
    parsed = urlsplit(uri)
    host = (parsed.hostname or "").lower()
    port = parsed.port or 7687
    return f"{parsed.scheme.lower()}://{host}:{port}{parsed.path}"
