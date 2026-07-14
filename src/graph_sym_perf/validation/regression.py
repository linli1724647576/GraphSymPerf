"""Fail-closed production gate for the frozen Neo4j BUG001 regression."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

import yaml

from graph_sym_perf.adapters.base import normalized_uri_endpoint
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
    to_jsonable,
)
from graph_sym_perf.gdsmith.replay import parse_gdsmith_replay_graph
from graph_sym_perf.runtime.models import EquivalentQueryPair
from graph_sym_perf.transforms.rule_models import RuleRegistry
from graph_sym_perf.transforms.safety_points import SafetyCertificate
from graph_sym_perf.validation.evidence import issue_validation_report_is_complete
from graph_sym_perf.validation.graph_snapshot import GraphSnapshot
from graph_sym_perf.validation.models import (
    CandidateBundle,
    IssueValidationReport,
    PipelineStage,
    ValidationPolicy,
    canonical_content_hash,
)
from graph_sym_perf.validation.session_runner import SpawnedValidationSessionRunner
from graph_sym_perf.validation.sealed_replay import replay_issue_validation_report
from graph_sym_perf.validation.validator import IssueValidator


_IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_QUERY_PREFIX_RE = re.compile(r"\b(?:PROFILE|EXPLAIN)\b", re.IGNORECASE)
_INDEX_NAME_RE = re.compile(
    r"CREATE\s+INDEX\s+(`?[A-Za-z_][A-Za-z0-9_]*`?)(?:\s+IF\s+NOT\s+EXISTS)?\s+FOR\b",
    re.IGNORECASE,
)
_CURRENT_VERSION_STATUSES = frozenset(
    {"not_checked", "reproduced", "not_reproduced", "inconclusive"}
)
_EXPECTED_INDEX_NAMES = tuple(f"i{index}" for index in range(7))
_EXPECTED_ENV_NAMES = {
    "image_digest_env": "GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST",
    "container_env": "GRAPHSYMPERF_NEO4J_BUG001_CONTAINER",
    "uri_env": "GRAPHSYMPERF_NEO4J_BUG001_URI",
    "user_env": "GRAPHSYMPERF_NEO4J_BUG001_USER",
    "password_env": "GRAPHSYMPERF_NEO4J_BUG001_PASSWORD",
}
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "dbms",
        "affected_version",
        "image_ref",
        *_EXPECTED_ENV_NAMES,
        "graph_path",
        "graph_sha256",
        "fast_query_path",
        "fast_query_sha256",
        "slow_query_path",
        "slow_query_sha256",
        "expected_slow_query",
        "canonical_rule_id",
        "expected_graph_nodes",
        "expected_graph_relationships",
    }
)
_FROZEN_MANIFEST_VALUES: Mapping[str, object] = {
    "schema_version": 1,
    "case_id": "neo4j_bug001_pattern_order",
    "dbms": "neo4j",
    "affected_version": "2026.04.0 community",
    "image_ref": "neo4j:2026.04.0-community",
    **_EXPECTED_ENV_NAMES,
    "graph_path": "reproducer_graph.cypher.txt",
    "graph_sha256": "718fd53bfffc8a16e6df1cc278272669bcf8d0edb89270df9f7e2e876062b6d9",
    "fast_query_path": "fast_query.cypher",
    "fast_query_sha256": "2e4f0682a4919567bc6f47d52ae5b3db774b3e7cb20e91bbfbd688e3aae297b6",
    "slow_query_path": "slow_query.cypher",
    "slow_query_sha256": "7d2b1528b6121bfbd4bf0352a50c10e709ff3d2fdeb1dfc425bd76c4fa0acdc2",
    "expected_slow_query": "b",
    "canonical_rule_id": "pattern_order_symmetry",
    "expected_graph_nodes": 1280,
    "expected_graph_relationships": 2290,
}
_RUNTIME_REASON_CODES = frozenset(
    {
        "ratio_below_threshold",
        "ci_lower_below_threshold",
        "delta_within_noise",
        "absolute_delta_within_noise",
        "direction_inconsistent",
        "direction_mismatch",
        "absolute_delta_below_threshold",
        "session_ratio_below_threshold",
        "pooled_ratio_below_threshold",
        "pooled_ratio_ci_below_threshold",
        "pooled_delta_below_threshold",
        "pooled_delta_ci_below_threshold",
        "magnitude_not_stable",
        "submission_thresholds_not_met",
        "runtime_inconclusive",
        "one_sided_timeout",
        "timeout_validation_confirmed",
        "known_regression_not_reproduced",
    }
)
_VERSION_SESSION_CODES = frozenset({"regression_affected_version_mismatch"})
_GRAPH_SESSION_CODES = frozenset(
    {"regression_index_state_mismatch", "graph_count_mismatch", "graph_fingerprint_mismatch"}
)
_INFRASTRUCTURE_REASON_CODES = frozenset(
    {
        "sessions_not_independent",
        "session_fingerprint_or_health_failure",
        "invalid_measurement_protocol",
        "validation_identity_mismatch",
        "validation_report_incomplete",
        "session_lifecycle_incomplete",
        "missing_paired_samples",
    }
)
_VERSION_SESSION_REASON_RE = re.compile(
    r"regression_affected_version_mismatch\|observed=(?P<observed>\d{4}\.\d{2}\.\d+ community)\Z"
)


class RegressionConfigurationError(ValueError):
    """The frozen case, environment, or provenance cannot be resolved safely."""


class RegressionArtifactError(ValueError):
    """The landed regression artifact is absent, malformed, stale, or failed."""


class RegressionVersionMismatch(RuntimeError):
    def __init__(self, expected: str, observed: str) -> None:
        super().__init__(f"regression_affected_version_mismatch|observed={observed}")
        self.expected = expected
        self.observed = observed


class RegressionGraphStateError(RuntimeError):
    def __init__(self, code: str = "regression_index_state_mismatch") -> None:
        super().__init__(code)
        self.code = code


class RegressionFailureKind(str, Enum):
    CONFIGURATION = "configuration"
    VERSION = "version"
    GRAPH = "graph"
    EQUIVALENCE = "equivalence"
    RUNTIME = "runtime"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class RegressionManifest:
    path: Path
    manifest_sha256: str
    schema_version: int
    case_id: str
    dbms: str
    affected_version: str
    image_ref: str
    image_digest_env: str
    container_env: str
    uri_env: str
    user_env: str
    password_env: str
    graph_path: Path
    graph_sha256: str
    fast_query_path: Path
    fast_query_sha256: str
    slow_query_path: Path
    slow_query_sha256: str
    expected_slow_query: str
    canonical_rule_id: str
    expected_graph_nodes: int
    expected_graph_relationships: int
    actual_graph_nodes: int
    actual_graph_relationships: int
    graph_snapshot: GraphSnapshot
    index_statements: tuple[str, ...]
    index_names: tuple[str, ...]
    fast_query: str
    slow_query: str

    @classmethod
    def load(cls, manifest_path: str | Path) -> "RegressionManifest":
        supplied = Path(manifest_path)
        if supplied.is_symlink():
            raise RegressionConfigurationError("regression manifest must not be a symlink")
        try:
            path = supplied.resolve(strict=True)
        except OSError as exc:
            raise RegressionConfigurationError("regression manifest not found") from exc
        if not path.is_file():
            raise RegressionConfigurationError("regression manifest not found")
        try:
            manifest_bytes = path.read_bytes()
            raw = yaml.safe_load(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RegressionConfigurationError("regression manifest is not readable YAML") from exc
        if not isinstance(raw, Mapping):
            raise RegressionConfigurationError("regression manifest root must be a mapping")
        keys = frozenset(raw)
        if keys != _MANIFEST_KEYS:
            unknown = sorted(str(key) for key in keys - _MANIFEST_KEYS)
            missing = sorted(_MANIFEST_KEYS - keys)
            detail = "unknown" if unknown else "missing"
            names = unknown or missing
            raise RegressionConfigurationError(
                f"regression manifest has {detail} fields: {','.join(names)}"
            )
        for name, expected in _FROZEN_MANIFEST_VALUES.items():
            if raw.get(name) != expected:
                raise RegressionConfigurationError(f"regression manifest field is not frozen: {name}")
        env_names = tuple(str(raw[name]) for name in _EXPECTED_ENV_NAMES)
        if len(set(env_names)) != len(env_names):
            raise RegressionConfigurationError("regression manifest environment names must be distinct")

        graph_path = _strict_fixture_path(path, str(raw["graph_path"]))
        fast_query_path = _strict_fixture_path(path, str(raw["fast_query_path"]))
        slow_query_path = _strict_fixture_path(path, str(raw["slow_query_path"]))
        graph_bytes = _read_fixture_bytes(graph_path, "graph_path")
        fast_bytes = _read_fixture_bytes(fast_query_path, "fast_query_path")
        slow_bytes = _read_fixture_bytes(slow_query_path, "slow_query_path")
        _require_hash(graph_bytes, str(raw["graph_sha256"]), "graph")
        _require_hash(fast_bytes, str(raw["fast_query_sha256"]), "fast query")
        _require_hash(slow_bytes, str(raw["slow_query_sha256"]), "slow query")
        try:
            graph_text = graph_bytes.decode("utf-8")
            fast_query = fast_bytes.decode("utf-8").strip()
            slow_query = slow_bytes.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RegressionConfigurationError("regression fixtures must be UTF-8") from exc
        _validate_query(fast_query, "fast_query_path")
        _validate_query(slow_query, "slow_query_path")
        if fast_query == slow_query:
            raise RegressionConfigurationError("fast and slow regression queries must differ")
        all_statements = tuple(line.strip() for line in graph_text.splitlines() if line.strip())
        indexed: list[tuple[str, str]] = []
        graph_statements: list[str] = []
        for statement in all_statements:
            match = _INDEX_NAME_RE.match(statement)
            if match is None:
                graph_statements.append(statement)
            else:
                indexed.append((match.group(1).strip("`"), statement))
        index_names = tuple(name for name, _statement in indexed)
        if index_names != _EXPECTED_INDEX_NAMES:
            raise RegressionConfigurationError("regression fixture indexes must be exactly i0..i6")
        try:
            parsed = parse_gdsmith_replay_graph(tuple(graph_statements))
        except (TypeError, ValueError) as exc:
            raise RegressionConfigurationError("graph fixture is not replayable GDsmith data") from exc
        if (
            len(parsed.nodes) != raw["expected_graph_nodes"]
            or len(parsed.relationships) != raw["expected_graph_relationships"]
        ):
            raise RegressionConfigurationError("regression graph counts do not match manifest")
        return cls(
            path=path,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            schema_version=int(raw["schema_version"]),
            case_id=str(raw["case_id"]),
            dbms=str(raw["dbms"]),
            affected_version=str(raw["affected_version"]),
            image_ref=str(raw["image_ref"]),
            image_digest_env=str(raw["image_digest_env"]),
            container_env=str(raw["container_env"]),
            uri_env=str(raw["uri_env"]),
            user_env=str(raw["user_env"]),
            password_env=str(raw["password_env"]),
            graph_path=graph_path,
            graph_sha256=str(raw["graph_sha256"]),
            fast_query_path=fast_query_path,
            fast_query_sha256=str(raw["fast_query_sha256"]),
            slow_query_path=slow_query_path,
            slow_query_sha256=str(raw["slow_query_sha256"]),
            expected_slow_query=str(raw["expected_slow_query"]),
            canonical_rule_id=str(raw["canonical_rule_id"]),
            expected_graph_nodes=int(raw["expected_graph_nodes"]),
            expected_graph_relationships=int(raw["expected_graph_relationships"]),
            actual_graph_nodes=len(parsed.nodes),
            actual_graph_relationships=len(parsed.relationships),
            graph_snapshot=GraphSnapshot.from_statements(tuple(graph_statements)),
            index_statements=tuple(statement for _name, statement in indexed),
            index_names=index_names,
            fast_query=fast_query,
            slow_query=slow_query,
        )


@dataclass(frozen=True)
class RegressionProvenance:
    verified_image_digest: str
    container_identity: str
    endpoint_identity: str
    main_endpoint_identity: str


@dataclass(frozen=True)
class RegressionCase:
    manifest: RegressionManifest
    image_digest: str
    container: str
    uri: str
    user: str = field(repr=False, compare=False)
    password: str = field(repr=False, compare=False)
    provenance: RegressionProvenance

    def __getattr__(self, name: str) -> Any:
        aliases = {
            "manifest_path": "path",
            "graph_file_sha256": "graph_sha256",
        }
        return getattr(self.manifest, aliases.get(name, name))


@dataclass(frozen=True)
class RegressionEnvironmentIdentity:
    observed_version: str
    dbms_fingerprint: str
    driver_version: str
    verified_image_digest: str = ""
    container_identity: str = ""
    endpoint_identity: str = ""
    index_names: tuple[str, ...] = _EXPECTED_INDEX_NAMES
    index_states: tuple[str, ...] = ("ONLINE",) * 7


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
    verified_image_digest: str
    container_identity: str
    endpoint_identity: str
    environment_identity_hash: str
    campaign_identity_hash: str
    manifest_sha256: str
    graph_file_sha256: str
    graph_snapshot_sha256: str
    fast_query_sha256: str
    slow_query_sha256: str
    expected_slow_query: str
    expected_index_names: tuple[str, ...]
    verified_index_states: tuple[str, ...]
    candidate_hash: str | None
    validation_report_hash: str | None
    policy_hash: str | None
    certificate_hash: str | None
    validation_evidence_sha256: str | None
    fresh_issue_count: int
    discovery_accepted_pairs: int
    validated_runtime_yield: int
    discovery_budget: int
    control_budget: int
    submit_ready_created: bool
    content_hash: str

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["failure_kind"] = self.failure_kind.value if self.failure_kind else None
        payload["reason_codes"] = list(self.reason_codes)
        payload["expected_index_names"] = list(self.expected_index_names)
        payload["verified_index_states"] = list(self.verified_index_states)
        return payload

    def verify_integrity(self) -> bool:
        payload = self.to_payload()
        content_hash = payload.pop("content_hash")
        zero_counts = (
            self.fresh_issue_count,
            self.discovery_accepted_pairs,
            self.validated_runtime_yield,
            self.discovery_budget,
            self.control_budget,
        )
        required_hashes = (
            self.environment_identity_hash,
            self.campaign_identity_hash,
            self.manifest_sha256,
            self.graph_file_sha256,
            self.graph_snapshot_sha256,
            self.fast_query_sha256,
            self.slow_query_sha256,
        )
        return bool(
            self.schema_version == 3
            and self.case_id == _FROZEN_MANIFEST_VALUES["case_id"]
            and self.known_regression is True
            and isinstance(self.passed, bool)
            and isinstance(self.affected_version_reproduced, bool)
            and self.affected_version == _FROZEN_MANIFEST_VALUES["affected_version"]
            and self.image_ref == _FROZEN_MANIFEST_VALUES["image_ref"]
            and self.current_version_status in _CURRENT_VERSION_STATUSES
            and all(isinstance(reason, str) and reason for reason in self.reason_codes)
            and all(type(value) is int and value == 0 for value in zero_counts)
            and self.submit_ready_created is False
            and self.expected_slow_query == "b"
            and self.expected_index_names == _EXPECTED_INDEX_NAMES
            and self.verified_index_states in {(), ("ONLINE",) * 7}
            and isinstance(self.verified_image_digest, str)
            and _IMAGE_DIGEST_RE.fullmatch(self.verified_image_digest)
            and all(
                isinstance(value, str) and _SHA256_RE.fullmatch(value)
                for value in required_hashes
            )
            and _optional_metrics_are_finite(
                self.validated_ratio,
                self.pooled_delta_ms,
            )
            and isinstance(content_hash, str)
            and _SHA256_RE.fullmatch(content_hash)
            and canonical_content_hash(payload) == content_hash
        )


@dataclass(frozen=True)
class RegressionConfigurationFailureArtifact:
    schema_version: int
    artifact_type: str
    case_id: str
    known_regression: bool
    passed: bool
    failure_kind: RegressionFailureKind
    reason_codes: tuple[str, ...]
    campaign_identity_hash: str
    manifest_sha256: str
    fresh_issue_count: int
    discovery_accepted_pairs: int
    validated_runtime_yield: int
    discovery_budget: int
    control_budget: int
    submit_ready_created: bool
    content_hash: str

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["failure_kind"] = self.failure_kind.value
        payload["reason_codes"] = list(self.reason_codes)
        return payload

    def verify_integrity(self) -> bool:
        payload = self.to_payload()
        content_hash = payload.pop("content_hash")
        return bool(
            self.schema_version == 1
            and self.artifact_type == "regression_configuration_failure"
            and self.case_id == _FROZEN_MANIFEST_VALUES["case_id"]
            and self.known_regression is True
            and self.passed is False
            and self.failure_kind is RegressionFailureKind.CONFIGURATION
            and len(self.reason_codes) == 1
            and re.fullmatch(
                r"regression_configuration_error:[A-Za-z_][A-Za-z0-9_]*",
                self.reason_codes[0],
            )
            and isinstance(self.campaign_identity_hash, str)
            and _SHA256_RE.fullmatch(self.campaign_identity_hash)
            and isinstance(self.manifest_sha256, str)
            and _SHA256_RE.fullmatch(self.manifest_sha256)
            and all(
                type(value) is int and value == 0
                for value in (
                    self.fresh_issue_count,
                    self.discovery_accepted_pairs,
                    self.validated_runtime_yield,
                    self.discovery_budget,
                    self.control_budget,
                )
            )
            and self.submit_ready_created is False
            and isinstance(content_hash, str)
            and _SHA256_RE.fullmatch(content_hash)
            and canonical_content_hash(payload) == content_hash
        )


class RegressionProvenanceProbe(Protocol):
    def __call__(self, environment: Mapping[str, str], target: TargetSystem) -> str | None: ...


class _IssueValidatorLike(Protocol):
    def validate(self, candidate: CandidateBundle, session_runner: Any) -> IssueValidationReport: ...


def load_regression_case(
    manifest_path: str | Path,
    environment: Mapping[str, str] | None = None,
    *,
    provenance_probe: RegressionProvenanceProbe | None = None,
) -> RegressionCase:
    """Resolve strict fixture, isolated endpoint, and live container provenance."""

    manifest = RegressionManifest.load(manifest_path)
    env = os.environ if environment is None else environment
    values = {
        name: _required_environment(env, getattr(manifest, name))
        for name in _EXPECTED_ENV_NAMES
    }
    image_digest = values["image_digest_env"]
    if _IMAGE_DIGEST_RE.fullmatch(image_digest) is None:
        raise RegressionConfigurationError(
            f"{manifest.image_digest_env} must match sha256:<64 lowercase hex>"
        )
    container = values["container_env"]
    uri = _validated_uri(values["uri_env"], manifest.uri_env)
    if urlsplit(uri).path:
        raise RegressionConfigurationError("BUG001 URI must not contain a database path")
    main_uri = _required_environment(env, "GRAPHSYMPERF_NEO4J_URI")
    normalized_main_uri = _validated_uri(main_uri, "GRAPHSYMPERF_NEO4J_URI")
    if normalized_main_uri == uri:
        raise RegressionConfigurationError("BUG001 URI must differ from the main Neo4j URI")
    main_database = env.get("GRAPHSYMPERF_NEO4J_DATABASE")
    dedicated_endpoint = dict(normalized_uri_endpoint(uri, default_port=7687))
    main_endpoint = dict(
        normalized_uri_endpoint(main_uri, default_port=7687, database=main_database)
    )
    dedicated_host_port = _uri_host_port(uri)
    main_host_port = _uri_host_port(normalized_main_uri)
    if (
        not dedicated_endpoint
        or dedicated_endpoint == main_endpoint
        or dedicated_host_port == main_host_port
    ):
        raise RegressionConfigurationError("BUG001 endpoint must differ from the main Neo4j endpoint")
    main_container = env.get("GRAPHSYMPERF_NEO4J_CONTAINER", "").strip()
    if main_container and main_container == container:
        raise RegressionConfigurationError("BUG001 container must differ from the main Neo4j container")
    probe = provenance_probe or _production_provenance_probe
    probe_environment = {
        "GRAPHSYMPERF_NEO4J_IMAGE_DIGEST": image_digest,
        "GRAPHSYMPERF_NEO4J_CONTAINER": container,
    }
    verified_digest = probe(probe_environment, TargetSystem.NEO4J)
    if verified_digest != image_digest:
        raise RegressionConfigurationError("BUG001 container image provenance mismatch")
    return RegressionCase(
        manifest=manifest,
        image_digest=image_digest,
        container=container,
        uri=uri,
        user=values["user_env"],
        password=values["password_env"],
        provenance=RegressionProvenance(
            verified_image_digest=verified_digest,
            container_identity=container,
            endpoint_identity=_endpoint_identity(dedicated_endpoint),
            main_endpoint_identity=_endpoint_identity(main_endpoint),
        ),
    )


@dataclass(frozen=True)
class RegressionNeo4jAdapterFactory:
    """Pickle-safe factory for the two fresh production validation sessions."""

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
                "image_digest": self.case.provenance.verified_image_digest,
                "container_identity": self.case.provenance.container_identity,
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
        delegate = self.adapter_creator(
            system,
            run_config,
            {
                "GRAPHSYMPERF_NEO4J_URI": self.case.uri,
                "GRAPHSYMPERF_NEO4J_USER": self.case.user,
                "GRAPHSYMPERF_NEO4J_PASSWORD": self.case.password,
            },
            allow_create=True,
        )
        return _AffectedVersionNeo4jAdapter(delegate, self.case)


class _AffectedVersionNeo4jAdapter:
    """Version-lock and verify all seven indexes in every fresh session."""

    def __init__(self, delegate: Any, case: RegressionCase) -> None:
        self._delegate = delegate
        self._case = case
        self.observed_version: str | None = None
        self.verified_index_states: tuple[str, ...] = ()

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
        version, edition = rows[0].get("version"), rows[0].get("edition")
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
        loaded = self._delegate.load_gdsmith_graph(statements)
        self.verify_indexes()
        return loaded

    def verify_indexes(self) -> tuple[str, ...]:
        """Read and verify the current frozen index identity without trusting an artifact."""

        self._delegate.execute(
            "CALL db.awaitIndexes(60)",
            {},
            "regression_index_await",
            max_rows=1,
            timeout_seconds=65.0,
        )
        result = self._delegate.execute(
            "SHOW INDEXES YIELD name, state RETURN name, state ORDER BY name",
            {},
            "regression_index_status",
            max_rows=100,
            timeout_seconds=60.0,
        )
        rows = getattr(result, "rows", ())
        statuses: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise RegressionGraphStateError()
            name, state = row.get("name"), row.get("state")
            if isinstance(name, str) and name in self._case.index_names and isinstance(state, str):
                statuses[name] = state.upper()
        if tuple(sorted(statuses)) != self._case.index_names:
            raise RegressionGraphStateError()
        states = tuple(statuses[name] for name in self._case.index_names)
        if states != ("ONLINE",) * len(self._case.index_names):
            raise RegressionGraphStateError()
        self.verified_index_states = states
        return states


def build_independent_session_runner(case: RegressionCase) -> SpawnedValidationSessionRunner:
    return SpawnedValidationSessionRunner(RegressionNeo4jAdapterFactory(case))


def _read_environment_identity(
    case: RegressionCase,
    *,
    verify_indexes: bool,
) -> RegressionEnvironmentIdentity:
    adapter = RegressionNeo4jAdapterFactory(case)(None)
    try:
        connected = adapter.connectivity_check()
        if not isinstance(connected, tuple) or not connected or connected[0] is not True:
            raise RuntimeError("regression_connectivity_unavailable")
        fingerprint = adapter.dbms_fingerprint()
        if not getattr(fingerprint, "available", False):
            raise RuntimeError("regression_dbms_fingerprint_unavailable")
        driver_version = getattr(fingerprint, "driver_version", None)
        if not isinstance(driver_version, str) or not driver_version:
            raise RuntimeError("regression_driver_version_unavailable")
        if not adapter.observed_version:
            raise RuntimeError("regression_version_identity_unavailable")
        index_states = adapter.verify_indexes() if verify_indexes else ("ONLINE",) * 7
        return RegressionEnvironmentIdentity(
            observed_version=adapter.observed_version,
            dbms_fingerprint=str(fingerprint.value),
            driver_version=driver_version,
            verified_image_digest=case.provenance.verified_image_digest,
            container_identity=case.provenance.container_identity,
            endpoint_identity=case.provenance.endpoint_identity,
            index_names=case.index_names,
            index_states=index_states,
        )
    finally:
        adapter.close()


def _probe_environment_identity(case: RegressionCase) -> RegressionEnvironmentIdentity:
    return _read_environment_identity(case, verify_indexes=False)


def probe_current_regression_environment_identity(
    case: RegressionCase,
) -> RegressionEnvironmentIdentity:
    """Probe the current affected endpoint, including the live i0..i6 index states."""

    try:
        return _read_environment_identity(case, verify_indexes=True)
    except Exception as exc:
        raise RegressionArtifactError("regression_current_environment_probe_failed") from exc


class RegressionRunner:
    """Production constructor is fixed to IssueValidator and spawned sessions."""

    def __init__(self, *, report_path: str | Path = "regression_report.json") -> None:
        self._validator: _IssueValidatorLike = IssueValidator()
        self.report_path = Path(report_path)
        self._identity_probe: Callable[[RegressionCase], RegressionEnvironmentIdentity] = (
            _probe_environment_identity
        )
        self._session_runner_factory: Callable[[RegressionCase], Any] = (
            build_independent_session_runner
        )

    @classmethod
    def _for_test(
        cls,
        validator: _IssueValidatorLike,
        *,
        report_path: str | Path,
        identity_probe: Callable[[RegressionCase], RegressionEnvironmentIdentity],
        session_runner_factory: Callable[[RegressionCase], Any],
    ) -> "RegressionRunner":
        runner = cls(report_path=report_path)
        runner._validator = validator
        runner._identity_probe = identity_probe
        runner._session_runner_factory = session_runner_factory
        return runner

    def run(
        self,
        case: RegressionCase,
        *,
        campaign_identity_hash: str,
        current_version_status: str = "not_checked",
    ) -> RegressionGateReport:
        if not isinstance(case, RegressionCase):
            raise TypeError("RegressionRunner requires a RegressionCase")
        if _SHA256_RE.fullmatch(campaign_identity_hash) is None:
            raise RegressionConfigurationError("campaign identity hash must be SHA-256")
        if current_version_status not in _CURRENT_VERSION_STATUSES:
            raise RegressionConfigurationError("invalid current_version_status")
        static_reasons = _static_graph_reasons(case)
        if static_reasons:
            return self._persist(
                _make_report(
                    case,
                    campaign_identity_hash=campaign_identity_hash,
                    passed=False,
                    failure_kind=RegressionFailureKind.GRAPH,
                    reason_codes=static_reasons,
                    current_version_status=current_version_status,
                )
            )
        try:
            identity = self._identity_probe(case)
        except RegressionVersionMismatch as exc:
            return self._persist(
                _make_report(
                    case,
                    campaign_identity_hash=campaign_identity_hash,
                    passed=False,
                    failure_kind=RegressionFailureKind.VERSION,
                    reason_codes=("regression_affected_version_mismatch",),
                    observed_version=exc.observed,
                    current_version_status=current_version_status,
                )
            )
        except Exception as exc:
            return self._persist(
                _make_report(
                    case,
                    campaign_identity_hash=campaign_identity_hash,
                    passed=False,
                    failure_kind=RegressionFailureKind.INFRASTRUCTURE,
                    reason_codes=(f"identity_infrastructure_error:{type(exc).__name__}",),
                    current_version_status=current_version_status,
                )
            )
        identity_reason = _identity_reason(case, identity)
        if identity_reason:
            kind = (
                RegressionFailureKind.VERSION
                if identity_reason == "regression_affected_version_mismatch"
                else RegressionFailureKind.INFRASTRUCTURE
            )
            return self._persist(
                _make_report(
                    case,
                    campaign_identity_hash=campaign_identity_hash,
                    passed=False,
                    failure_kind=kind,
                    reason_codes=(identity_reason,),
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
                    campaign_identity_hash=campaign_identity_hash,
                    passed=False,
                    failure_kind=RegressionFailureKind.INFRASTRUCTURE,
                    reason_codes=(f"validation_infrastructure_error:{type(exc).__name__}",),
                    observed_version=identity.observed_version,
                    identity=identity,
                    current_version_status=current_version_status,
                )
            )
        exact_report = isinstance(validation, IssueValidationReport)
        identity_matches = bool(
            exact_report
            and validation.candidate_hash == candidate.candidate_hash
            and validation.query_a_hash == candidate.content_hashes["query_a"]
            and validation.query_b_hash == candidate.content_hashes["query_b"]
            and validation.policy_hash == candidate.content_hashes["validation_policy"]
            and validation.certificate_hash == candidate.content_hashes["certificate"]
            and validation.rule_id == candidate.certificate.rule_id
            and validation.proof_version == candidate.certificate.proof_id
        )
        complete = bool(
            exact_report
            and identity_matches
            and issue_validation_report_is_complete(validation)
            and _validation_metrics_are_finite(validation)
        )
        reasons = tuple(getattr(validation, "reason_codes", ())) if exact_report else ()
        if not complete and not reasons:
            reasons = (
                "validation_identity_mismatch"
                if exact_report and not identity_matches
                else "validation_report_incomplete"
            )
        failure_kind = None if complete else _validation_failure_kind(validation)
        report = _make_report(
            case,
            campaign_identity_hash=campaign_identity_hash,
            passed=complete,
            failure_kind=failure_kind,
            reason_codes=reasons,
            observed_version=(
                _session_observed_version(validation) or identity.observed_version
            ),
            identity=identity,
            current_version_status=current_version_status,
            validation=validation if exact_report else None,
            candidate=candidate,
        )
        return self._persist(
            report,
            case=case if complete else None,
            validation=validation if complete and exact_report else None,
            candidate=candidate if complete else None,
        )

    def _persist(
        self,
        report: RegressionGateReport,
        *,
        case: RegressionCase | None = None,
        validation: IssueValidationReport | None = None,
        candidate: CandidateBundle | None = None,
    ) -> RegressionGateReport:
        if report.passed:
            if case is None or validation is None or candidate is None:
                raise RegressionArtifactError("passing regression report requires typed evidence")
            evidence_payload = _validation_evidence_payload(case, candidate, validation)
            evidence_sha256 = _serialized_payload_sha256(evidence_payload)
            if evidence_sha256 != report.validation_evidence_sha256:
                raise RegressionArtifactError("regression evidence binding differs")
            _atomic_write_payload(
                regression_validation_evidence_path(self.report_path),
                evidence_payload,
            )
        write_regression_report(report, self.report_path)
        return report


def build_production_regression_runner(report_path: str | Path) -> RegressionRunner:
    return RegressionRunner(report_path=report_path)


def regression_campaign_identity(
    *,
    spec_name: str,
    schema_version: int,
    manifest_sha256: str,
) -> str:
    return canonical_content_hash(
        {
            "protocol": "known-regression-gate-v2",
            "spec_name": spec_name,
            "schema_version": schema_version,
            "target": TargetSystem.NEO4J.value,
            "case_id": _FROZEN_MANIFEST_VALUES["case_id"],
            "manifest_sha256": manifest_sha256,
        }
    )


def regression_environment_identity_hash(case: RegressionCase) -> str:
    return canonical_content_hash(
        {
            "affected_version": case.affected_version,
            "image_ref": case.image_ref,
            "verified_image_digest": case.provenance.verified_image_digest,
            "container_identity": case.provenance.container_identity,
            "endpoint_identity": case.provenance.endpoint_identity,
            "main_endpoint_identity": case.provenance.main_endpoint_identity,
            "expected_index_names": case.index_names,
        }
    )


def write_regression_configuration_failure(
    *,
    report_path: str | Path,
    manifest: RegressionManifest,
    campaign_identity_hash: str,
    exception_type: str,
) -> None:
    write_unresolved_regression_configuration_failure(
        report_path=report_path,
        case_id=manifest.case_id,
        manifest_sha256=manifest.manifest_sha256,
        campaign_identity_hash=campaign_identity_hash,
        exception_type=exception_type,
    )


def write_unresolved_regression_configuration_failure(
    *,
    report_path: str | Path,
    case_id: str,
    manifest_sha256: str,
    campaign_identity_hash: str,
    exception_type: str,
) -> None:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", exception_type) is None:
        raise RegressionConfigurationError("invalid configuration exception type")
    if case_id != _FROZEN_MANIFEST_VALUES["case_id"]:
        raise RegressionConfigurationError("invalid regression case identity")
    if (
        _SHA256_RE.fullmatch(manifest_sha256) is None
        or _SHA256_RE.fullmatch(campaign_identity_hash) is None
    ):
        raise RegressionConfigurationError("invalid configuration artifact identity")
    values: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "regression_configuration_failure",
        "case_id": case_id,
        "known_regression": True,
        "passed": False,
        "failure_kind": RegressionFailureKind.CONFIGURATION,
        "reason_codes": (f"regression_configuration_error:{exception_type}",),
        "campaign_identity_hash": campaign_identity_hash,
        "manifest_sha256": manifest_sha256,
        "fresh_issue_count": 0,
        "discovery_accepted_pairs": 0,
        "validated_runtime_yield": 0,
        "discovery_budget": 0,
        "control_budget": 0,
        "submit_ready_created": False,
    }
    hash_payload = {
        **values,
        "failure_kind": RegressionFailureKind.CONFIGURATION.value,
        "reason_codes": list(values["reason_codes"]),
    }
    artifact = RegressionConfigurationFailureArtifact(
        **values,
        content_hash=canonical_content_hash(hash_payload),
    )
    _atomic_write_payload(Path(report_path), artifact.to_payload())


def load_regression_gate_artifact(
    report_path: str | Path,
) -> RegressionGateReport | RegressionConfigurationFailureArtifact:
    path = Path(report_path)
    if path.is_symlink() or not path.is_file():
        raise RegressionArtifactError("regression_gate_artifact_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegressionArtifactError("regression_gate_artifact_unreadable") from exc
    if not isinstance(payload, Mapping):
        raise RegressionArtifactError("regression_gate_artifact_invalid")
    if payload.get("artifact_type") == "regression_configuration_failure":
        expected_keys = frozenset(
            field.name
            for field in RegressionConfigurationFailureArtifact.__dataclass_fields__.values()
        )
        if frozenset(payload) != expected_keys:
            raise RegressionArtifactError("regression_gate_artifact_schema_mismatch")
        try:
            artifact = RegressionConfigurationFailureArtifact(
                **{
                    **dict(payload),
                    "failure_kind": RegressionFailureKind(str(payload["failure_kind"])),
                    "reason_codes": tuple(payload["reason_codes"]),
                }
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise RegressionArtifactError("regression_gate_artifact_schema_mismatch") from exc
        if not artifact.verify_integrity():
            raise RegressionArtifactError("regression_gate_artifact_hash_mismatch")
        return artifact
    return _regression_gate_report_from_payload(payload)


def _regression_gate_report_from_payload(
    payload: Mapping[str, object],
) -> RegressionGateReport:
    expected_keys = frozenset(
        field.name for field in RegressionGateReport.__dataclass_fields__.values()
    )
    if frozenset(payload) != expected_keys:
        raise RegressionArtifactError("regression_gate_artifact_schema_mismatch")
    try:
        report = RegressionGateReport(
            **{
                **dict(payload),
                "failure_kind": (
                    RegressionFailureKind(str(payload["failure_kind"]))
                    if payload["failure_kind"] is not None
                    else None
                ),
                "reason_codes": tuple(payload["reason_codes"]),
                "expected_index_names": tuple(payload["expected_index_names"]),
                "verified_index_states": tuple(payload["verified_index_states"]),
            }
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise RegressionArtifactError("regression_gate_artifact_schema_mismatch") from exc
    if not report.verify_integrity():
        raise RegressionArtifactError("regression_gate_artifact_hash_mismatch")
    return report


def load_verified_regression_report(
    report_path: str | Path,
    *,
    case: RegressionCase,
    campaign_identity_hash: str,
    current_identity: RegressionEnvironmentIdentity,
) -> RegressionGateReport:
    artifact = load_regression_gate_artifact(report_path)
    if not isinstance(artifact, RegressionGateReport):
        raise RegressionArtifactError("regression_gate_artifact_failed")
    report = artifact
    expected = {
        "case_id": case.case_id,
        "affected_version": case.affected_version,
        "image_ref": case.image_ref,
        "verified_image_digest": case.provenance.verified_image_digest,
        "container_identity": case.provenance.container_identity,
        "endpoint_identity": case.provenance.endpoint_identity,
        "environment_identity_hash": regression_environment_identity_hash(case),
        "campaign_identity_hash": campaign_identity_hash,
        "manifest_sha256": case.manifest_sha256,
        "graph_file_sha256": case.graph_file_sha256,
        "graph_snapshot_sha256": case.graph_snapshot.content_hash,
        "expected_slow_query": case.expected_slow_query,
        "expected_index_names": case.index_names,
        "observed_version": current_identity.observed_version,
        "dbms_fingerprint": current_identity.dbms_fingerprint,
        "driver_version": current_identity.driver_version,
    }
    if (
        _identity_reason(case, current_identity) is not None
        or current_identity.verified_image_digest
        != case.provenance.verified_image_digest
        or current_identity.container_identity != case.provenance.container_identity
        or current_identity.endpoint_identity != case.provenance.endpoint_identity
        or any(getattr(report, name) != value for name, value in expected.items())
    ):
        raise RegressionArtifactError("regression_gate_artifact_identity_mismatch")
    if (
        report.passed is not True
        or report.failure_kind is not None
        or report.reason_codes
        or report.affected_version_reproduced is not True
        or report.validation_stage != PipelineStage.VALIDATION_PASSED.value
        or report.validation_protocol != "independent-two-session-v1"
        or report.verified_index_states != ("ONLINE",) * len(case.index_names)
        or not all(
            isinstance(value, str) and _SHA256_RE.fullmatch(value)
            for value in (
                report.candidate_hash,
                report.validation_report_hash,
                report.policy_hash,
                report.certificate_hash,
                report.validation_evidence_sha256,
            )
        )
        or not _optional_metrics_are_finite(report.validated_ratio, report.pooled_delta_ms)
    ):
        raise RegressionArtifactError("regression_gate_artifact_failed")
    candidate = _candidate(case, current_identity)
    evidence_payload, evidence_file_sha256 = _load_validation_evidence_payload(
        report_path,
        case=case,
    )
    if (
        evidence_file_sha256 != report.validation_evidence_sha256
        or evidence_payload["candidate_hash"] != candidate.candidate_hash
    ):
        raise RegressionArtifactError("regression_gate_evidence_identity_mismatch")
    raw_report = evidence_payload["report"]
    try:
        recomputed = replay_issue_validation_report(candidate, raw_report)
    except (KeyError, TypeError, ValueError) as exc:
        raise RegressionArtifactError("regression_gate_evidence_replay_failed") from exc
    validation_report_hash = canonical_content_hash(raw_report)
    exact_bindings = {
        "candidate_hash": candidate.candidate_hash,
        "validation_report_hash": validation_report_hash,
        "policy_hash": str(candidate.content_hashes["validation_policy"]),
        "certificate_hash": str(candidate.content_hashes["certificate"]),
        "validation_stage": recomputed.stage.value,
        "validation_protocol": recomputed.validation_protocol,
        "validated_ratio": recomputed.validated_ratio,
        "pooled_delta_ms": recomputed.pooled_delta_ms,
    }
    if (
        recomputed.stage is not PipelineStage.VALIDATION_PASSED
        or not issue_validation_report_is_complete(recomputed)
        or not _validation_metrics_are_finite(recomputed)
        or any(getattr(report, name) != value for name, value in exact_bindings.items())
    ):
        raise RegressionArtifactError("regression_gate_evidence_identity_mismatch")
    return report


def regression_validation_evidence_path(report_path: str | Path) -> Path:
    """Return the fixed typed-evidence sidecar within the frozen case directory."""

    return Path(report_path).parent / "validation_evidence.json"


def _validation_evidence_payload(
    case: RegressionCase,
    candidate: CandidateBundle,
    validation: IssueValidationReport,
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": 1,
        "artifact_type": "regression_validation_evidence",
        "case_id": case.case_id,
        "candidate_hash": candidate.candidate_hash,
        "report": to_jsonable(asdict(validation)),
    }
    return {**body, "content_hash": canonical_content_hash(body)}


def _load_validation_evidence_payload(
    report_path: str | Path,
    *,
    case: RegressionCase,
) -> tuple[dict[str, object], str]:
    path = regression_validation_evidence_path(report_path)
    if path.is_symlink() or not path.is_file():
        raise RegressionArtifactError("regression_gate_evidence_missing")
    try:
        raw_bytes = path.read_bytes()
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegressionArtifactError("regression_gate_evidence_unreadable") from exc
    expected_keys = {
        "schema_version",
        "artifact_type",
        "case_id",
        "candidate_hash",
        "report",
        "content_hash",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise RegressionArtifactError("regression_gate_evidence_schema_mismatch")
    body = {name: value for name, value in payload.items() if name != "content_hash"}
    try:
        canonical_hash = canonical_content_hash(body)
    except (TypeError, ValueError) as exc:
        raise RegressionArtifactError("regression_gate_evidence_noncanonical") from exc
    if (
        payload["schema_version"] != 1
        or payload["artifact_type"] != "regression_validation_evidence"
        or payload["case_id"] != case.case_id
        or not isinstance(payload["candidate_hash"], str)
        or _SHA256_RE.fullmatch(payload["candidate_hash"]) is None
        or not isinstance(payload["report"], dict)
        or payload["content_hash"] != canonical_hash
    ):
        raise RegressionArtifactError("regression_gate_evidence_hash_mismatch")
    return payload, hashlib.sha256(raw_bytes).hexdigest()


def _candidate(case: RegressionCase, identity: RegressionEnvironmentIdentity) -> CandidateBundle:
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
            "verified_image_digest": case.provenance.verified_image_digest,
            "container_identity": case.provenance.container_identity,
            "endpoint_identity": case.provenance.endpoint_identity,
            "expected_index_names": case.index_names,
            "verified_index_states": ("ONLINE",) * len(case.index_names),
            "volatile_functions": (),
        },
        discovery_slow_query=case.expected_slow_query,
        discovery_ratio=3.0,
        certificate=certificate,
        validation_policy=ValidationPolicy(
            max_result_rows=100_000,
            query_timeout_seconds=60.0,
            session_timeout_seconds=600.0,
        ),
    )


def _make_report(
    case: RegressionCase,
    *,
    campaign_identity_hash: str,
    passed: bool,
    failure_kind: RegressionFailureKind | None,
    reason_codes: tuple[str, ...],
    current_version_status: str,
    observed_version: str | None = None,
    identity: RegressionEnvironmentIdentity | None = None,
    validation: IssueValidationReport | None = None,
    candidate: CandidateBundle | None = None,
) -> RegressionGateReport:
    validation_is_finite = bool(
        validation is not None and _validation_metrics_are_finite(validation)
    )
    values: dict[str, Any] = {
        "schema_version": 3,
        "case_id": case.case_id,
        "known_regression": True,
        "passed": passed,
        "affected_version_reproduced": passed,
        "failure_kind": failure_kind,
        "reason_codes": reason_codes,
        "affected_version": case.affected_version,
        "observed_version": observed_version,
        "current_version_status": current_version_status,
        "validation_stage": validation.stage.value if validation is not None else None,
        "validation_protocol": (
            validation.validation_protocol
            if validation is not None
            else "independent-two-session-v1"
        ),
        "validated_ratio": (
            validation.validated_ratio if validation_is_finite and validation is not None else None
        ),
        "pooled_delta_ms": (
            validation.pooled_delta_ms if validation_is_finite and validation is not None else None
        ),
        "dbms_fingerprint": identity.dbms_fingerprint if identity else None,
        "driver_version": identity.driver_version if identity else None,
        "image_ref": case.image_ref,
        "verified_image_digest": case.provenance.verified_image_digest,
        "container_identity": case.provenance.container_identity,
        "endpoint_identity": case.provenance.endpoint_identity,
        "environment_identity_hash": regression_environment_identity_hash(case),
        "campaign_identity_hash": campaign_identity_hash,
        "manifest_sha256": case.manifest_sha256,
        "graph_file_sha256": case.graph_file_sha256,
        "graph_snapshot_sha256": case.graph_snapshot.content_hash,
        "fast_query_sha256": hashlib.sha256(case.fast_query.encode("utf-8")).hexdigest(),
        "slow_query_sha256": hashlib.sha256(case.slow_query.encode("utf-8")).hexdigest(),
        "expected_slow_query": case.expected_slow_query,
        "expected_index_names": case.index_names,
        "verified_index_states": ("ONLINE",) * len(case.index_names) if passed else (),
        "candidate_hash": candidate.candidate_hash if candidate else None,
        "validation_report_hash": (
            canonical_content_hash(to_jsonable(validation)) if validation_is_finite else None
        ),
        "policy_hash": (
            str(candidate.content_hashes["validation_policy"]) if candidate else None
        ),
        "certificate_hash": (
            str(candidate.content_hashes["certificate"]) if candidate else None
        ),
        "validation_evidence_sha256": (
            _serialized_payload_sha256(
                _validation_evidence_payload(case, candidate, validation)
            )
            if passed and validation is not None and candidate is not None
            else None
        ),
        "fresh_issue_count": 0,
        "discovery_accepted_pairs": 0,
        "validated_runtime_yield": 0,
        "discovery_budget": 0,
        "control_budget": 0,
        "submit_ready_created": False,
    }
    payload = {
        **values,
        "failure_kind": failure_kind.value if failure_kind else None,
        "reason_codes": list(reason_codes),
        "expected_index_names": list(values["expected_index_names"]),
        "verified_index_states": list(values["verified_index_states"]),
    }
    return RegressionGateReport(**values, content_hash=canonical_content_hash(payload))


def _static_graph_reasons(case: RegressionCase) -> tuple[str, ...]:
    try:
        manifest = RegressionManifest.load(case.manifest_path)
    except RegressionConfigurationError:
        return ("graph_fixture_drift",)
    if (
        manifest.manifest_sha256 != case.manifest_sha256
        or manifest.graph_sha256 != case.graph_file_sha256
        or manifest.graph_snapshot.content_hash != case.graph_snapshot.content_hash
    ):
        return ("graph_fixture_drift",)
    return ()


def _identity_reason(case: RegressionCase, identity: RegressionEnvironmentIdentity) -> str | None:
    if identity.observed_version != case.affected_version:
        return "regression_affected_version_mismatch"
    if _SHA256_RE.fullmatch(identity.dbms_fingerprint) is None or not identity.driver_version.strip():
        return "invalid_environment_identity"
    if identity.verified_image_digest not in {"", case.provenance.verified_image_digest}:
        return "invalid_environment_identity"
    if identity.container_identity not in {"", case.provenance.container_identity}:
        return "invalid_environment_identity"
    if identity.endpoint_identity not in {"", case.provenance.endpoint_identity}:
        return "invalid_environment_identity"
    if identity.index_names != case.index_names or identity.index_states != ("ONLINE",) * 7:
        return "invalid_environment_identity"
    return None


def _validation_failure_kind(validation: object) -> RegressionFailureKind:
    if not isinstance(validation, IssueValidationReport):
        return RegressionFailureKind.INFRASTRUCTURE
    if any(reason in _INFRASTRUCTURE_REASON_CODES for reason in validation.reason_codes):
        return RegressionFailureKind.INFRASTRUCTURE
    session_reasons = tuple(
        str(reason)
        for session in validation.sessions
        for reason in session.evidence.reason_codes
    )
    if any(
        reason in _VERSION_SESSION_CODES or _VERSION_SESSION_REASON_RE.fullmatch(reason)
        for reason in session_reasons
    ):
        return RegressionFailureKind.VERSION
    if any(reason in _GRAPH_SESSION_CODES for reason in session_reasons):
        return RegressionFailureKind.GRAPH
    if any(
        session.lifecycle is not None
        and session.lifecycle.graph_loaded
        and not session.lifecycle.graph_verified
        for session in validation.sessions
    ):
        return RegressionFailureKind.GRAPH
    if any(
        session.lifecycle is None or not session.lifecycle.complete
        for session in validation.sessions
    ):
        return RegressionFailureKind.INFRASTRUCTURE
    if not validation.equivalence.equivalent or not validation.equivalence.complete_result:
        return RegressionFailureKind.EQUIVALENCE
    if validation.reason_codes and set(validation.reason_codes) <= _RUNTIME_REASON_CODES:
        return RegressionFailureKind.RUNTIME
    return RegressionFailureKind.INFRASTRUCTURE


def _session_observed_version(validation: object) -> str | None:
    if not isinstance(validation, IssueValidationReport):
        return None
    observed = {
        match.group("observed")
        for session in validation.sessions
        for reason in session.evidence.reason_codes
        if (match := _VERSION_SESSION_REASON_RE.fullmatch(str(reason))) is not None
    }
    return next(iter(observed)) if len(observed) == 1 else None


def _validation_metrics_are_finite(report: IssueValidationReport) -> bool:
    scalars = (
        report.discovery_ratio,
        report.validated_ratio,
        report.pooled_delta_ms,
        report.pooled_ratio_ci_low,
        report.pooled_delta_ci_low_ms,
    )
    if not _optional_metrics_are_finite(*scalars):
        return False
    for session in report.sessions:
        evidences = (session.evidence, *session.negative_controls)
        for evidence in evidences:
            if not _optional_metrics_are_finite(
                evidence.median_ratio,
                evidence.median_delta_ms,
                evidence.ci_ratio_low,
                evidence.ci_ratio_high,
                evidence.direction_consistency,
                evidence.relative_mad,
                evidence.ci_delta_low_ms,
                evidence.ci_delta_high_ms,
            ):
                return False
            for sample in evidence.paired_samples:
                if not _optional_metrics_are_finite(sample.a_ms, sample.b_ms):
                    return False
    return True


def _optional_metrics_are_finite(*values: object) -> bool:
    return all(value is None or isinstance(value, int | float) and math.isfinite(value) for value in values)


def write_regression_report(report: RegressionGateReport, path: str | Path) -> None:
    if not isinstance(report, RegressionGateReport) or not report.verify_integrity():
        raise RegressionArtifactError("cannot persist invalid regression report")
    _atomic_write_payload(Path(path), report.to_payload())


def _atomic_write_payload(destination: Path, payload: Mapping[str, object]) -> None:
    _mkdir_parents_durable(destination.parent)
    serialized = _serialized_payload(payload)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _serialized_payload(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _serialized_payload_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_serialized_payload(payload)).hexdigest()


def _mkdir_parents_durable(directory: Path) -> None:
    missing: list[Path] = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    directory.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        _fsync_directory(created)
        _fsync_directory(created.parent)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _strict_fixture_path(manifest_path: Path, name: str) -> Path:
    relative = Path(name)
    candidate = manifest_path.parent / relative
    if relative.is_absolute() or len(relative.parts) != 1:
        raise RegressionConfigurationError("regression fixture paths must be exact local filenames")
    if candidate.is_symlink():
        raise RegressionConfigurationError("regression fixture must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RegressionConfigurationError("regression fixture not found") from exc
    if resolved.parent != manifest_path.parent.resolve() or not resolved.is_file():
        raise RegressionConfigurationError("regression fixture escaped its manifest directory")
    return resolved


def _read_fixture_bytes(path: Path, field_name: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RegressionConfigurationError(f"cannot read {field_name} fixture") from exc


def _require_hash(value: bytes, expected: str, label: str) -> None:
    if _SHA256_RE.fullmatch(expected) is None or hashlib.sha256(value).hexdigest() != expected:
        raise RegressionConfigurationError(f"{label} fixture SHA-256 mismatch")


def _validate_query(query: str, field_name: str) -> None:
    if not query:
        raise RegressionConfigurationError(f"{field_name} query fixture is empty")
    if _FORBIDDEN_QUERY_PREFIX_RE.search(query):
        raise RegressionConfigurationError(f"{field_name} must not contain PROFILE or EXPLAIN")


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RegressionConfigurationError(f"required regression environment is missing: {name}")
    return value.strip()


def _validated_uri(value: str, env_name: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RegressionConfigurationError(f"{env_name} is not a valid Neo4j URI") from exc
    if parsed.scheme.lower() not in {
        "bolt",
        "bolt+s",
        "bolt+ssc",
        "neo4j",
        "neo4j+s",
        "neo4j+ssc",
    }:
        raise RegressionConfigurationError(f"{env_name} must use a Neo4j driver URI scheme")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise RegressionConfigurationError(f"{env_name} must not embed credentials")
    if parsed.query or parsed.fragment:
        raise RegressionConfigurationError(f"{env_name} must not contain query or fragment data")
    netloc = parsed.hostname
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _uri_host_port(uri: str) -> tuple[str, int]:
    parsed = urlsplit(uri)
    if parsed.hostname is None:  # pragma: no cover - guarded by _validated_uri
        raise RegressionConfigurationError("Neo4j URI hostname is unavailable")
    return parsed.hostname.lower(), parsed.port or 7687


def _endpoint_identity(endpoint: Mapping[str, object]) -> str:
    return canonical_content_hash(dict(endpoint))


def _production_provenance_probe(
    environment: Mapping[str, str],
    target: TargetSystem,
) -> str | None:
    # Lazy import avoids the experiment package's public re-export cycle while
    # still using the single production docker-inspect implementation.
    from graph_sym_perf.experiment.provenance import verified_container_image_digest

    return verified_container_image_digest(environment, target)


__all__ = [
    "RegressionArtifactError",
    "RegressionCase",
    "RegressionConfigurationError",
    "RegressionConfigurationFailureArtifact",
    "RegressionEnvironmentIdentity",
    "RegressionFailureKind",
    "RegressionGateReport",
    "RegressionGraphStateError",
    "RegressionManifest",
    "RegressionNeo4jAdapterFactory",
    "RegressionProvenance",
    "RegressionProvenanceProbe",
    "RegressionRunner",
    "RegressionVersionMismatch",
    "build_independent_session_runner",
    "build_production_regression_runner",
    "load_regression_case",
    "load_regression_gate_artifact",
    "load_verified_regression_report",
    "probe_current_regression_environment_identity",
    "regression_campaign_identity",
    "regression_environment_identity_hash",
    "regression_validation_evidence_path",
    "write_regression_configuration_failure",
    "write_unresolved_regression_configuration_failure",
    "write_regression_report",
]
