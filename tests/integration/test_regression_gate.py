from __future__ import annotations

import copy
import hashlib
import json
import pickle
import re
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import graph_sym_perf.validation.regression as regression_module
from graph_sym_perf.core import ExecutionResult, TargetSystem
from graph_sym_perf.validation.models import (
    CandidateBundle,
    EquivalenceEvidence,
    PipelineStage,
    ValidationPolicy,
    canonical_content_hash,
)
from graph_sym_perf.validation.regression import (
    RegressionArtifactError,
    RegressionConfigurationError,
    RegressionEnvironmentIdentity,
    RegressionFailureKind,
    RegressionGraphStateError,
    RegressionManifest,
    RegressionRunner,
    _AffectedVersionNeo4jAdapter,
    build_independent_session_runner,
    load_regression_case,
    load_verified_regression_report,
    regression_campaign_identity,
    regression_validation_evidence_path,
)
from graph_sym_perf.validation.session_runner import SpawnedValidationSessionRunner
from graph_sym_perf.validation.session_runner import validation_schedule_seed
from graph_sym_perf.validation.validator import IssueValidator
from tests.integration.test_issue_validation import FakeIndependentRunner


FIXTURE = Path("tests/fixtures/regressions/neo4j_bug001/manifest.yaml")
GRAPH_SHA256 = "718fd53bfffc8a16e6df1cc278272669bcf8d0edb89270df9f7e2e876062b6d9"
IMAGE_DIGEST = "sha256:" + "a" * 64
PASSWORD = "regression-secret-must-not-leak"


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST": IMAGE_DIGEST,
        "GRAPHSYMPERF_NEO4J_BUG001_CONTAINER": "neo4j-bug001",
        "GRAPHSYMPERF_NEO4J_BUG001_URI": "bolt://127.0.0.1:17687",
        "GRAPHSYMPERF_NEO4J_BUG001_USER": "neo4j-bug001-user",
        "GRAPHSYMPERF_NEO4J_BUG001_PASSWORD": PASSWORD,
        "GRAPHSYMPERF_NEO4J_URI": "bolt://127.0.0.1:7687/main",
        "GRAPHSYMPERF_NEO4J_DATABASE": "main",
        "GRAPHSYMPERF_NEO4J_CONTAINER": "neo4j-main",
    }
    values.update(overrides)
    return values


def _case(**overrides: str):
    return load_regression_case(
        FIXTURE,
        _environment(**overrides),
        provenance_probe=lambda environment, target: (
            environment["GRAPHSYMPERF_NEO4J_IMAGE_DIGEST"]
            if target is TargetSystem.NEO4J
            else None
        ),
    )


def test_regression_case_is_pickle_safe_for_spawned_validation_sessions() -> None:
    case = _case()

    restored = pickle.loads(pickle.dumps(case))

    assert restored == case
    assert restored.manifest_path == case.manifest_path
    assert restored.password == case.password


def _identity(case=None, *, version: str = "2026.04.0 community"):
    case = case or _case()
    return RegressionEnvironmentIdentity(
        observed_version=version,
        dbms_fingerprint="b" * 64,
        driver_version="6.0.0",
        verified_image_digest=case.provenance.verified_image_digest,
        container_identity=case.provenance.container_identity,
        endpoint_identity=case.provenance.endpoint_identity,
        index_names=case.index_names,
        index_states=("ONLINE",) * 7,
    )


def _campaign(case) -> str:
    return regression_campaign_identity(
        spec_name="neo4j_validated_runtime_yield",
        schema_version=3,
        manifest_sha256=case.manifest_sha256,
    )


class _SeededFakeIndependentRunner(FakeIndependentRunner):
    def run_session(self, candidate, session_number, **kwargs):
        equivalence, session = super().run_session(candidate, session_number, **kwargs)
        return equivalence, replace(
            session,
            schedule_seed=validation_schedule_seed(candidate.candidate_hash, session_number),
        )


def _runner(case, report_path: Path, validator=None, session_runner=None):
    return RegressionRunner._for_test(
        validator or IssueValidator(),
        report_path=report_path,
        identity_probe=lambda _case: _identity(case),
        session_runner_factory=lambda _case: session_runner or _SeededFakeIndependentRunner(),
    )


def _copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "neo4j_bug001"
    destination.mkdir(parents=True)
    for source in FIXTURE.parent.iterdir():
        if source.is_file():
            shutil.copy2(source, destination / source.name)
    return destination / "manifest.yaml"


def _mutate_manifest(path: Path, **updates: object) -> None:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload.update(updates)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _tampered_report(report, **changes: object):
    tampered = copy.copy(report)
    for name, value in changes.items():
        object.__setattr__(tampered, name, value)
    return tampered


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _rebind_sidecar_and_main(
    report_path: Path,
    sidecar_payload: dict[str, object],
    *,
    rehash_sidecar: bool = True,
) -> None:
    if rehash_sidecar:
        sidecar_body = {
            name: value
            for name, value in sidecar_payload.items()
            if name != "content_hash"
        }
        sidecar_payload["content_hash"] = canonical_content_hash(sidecar_body)
    sidecar_bytes = _canonical_json_bytes(sidecar_payload)
    regression_validation_evidence_path(report_path).write_bytes(sidecar_bytes)
    main_payload = json.loads(report_path.read_text(encoding="utf-8"))
    main_payload["validation_evidence_sha256"] = hashlib.sha256(sidecar_bytes).hexdigest()
    main_body = {
        name: value for name, value in main_payload.items() if name != "content_hash"
    }
    main_payload["content_hash"] = canonical_content_hash(main_body)
    report_path.write_bytes(_canonical_json_bytes(main_payload))


class FixedReportValidator:
    def __init__(self, transform=None) -> None:
        self.transform = transform or (lambda report, _candidate: report)

    def validate(self, candidate, _session_runner):
        report = IssueValidator().validate(candidate, FakeIndependentRunner())
        return self.transform(report, candidate)


def test_frozen_fixture_and_query_direction_are_exact() -> None:
    manifest = RegressionManifest.load(FIXTURE)
    case = _case()

    assert manifest.graph_sha256 == GRAPH_SHA256
    assert (manifest.actual_graph_nodes, manifest.actual_graph_relationships) == (1280, 2290)
    assert manifest.index_names == tuple(f"i{index}" for index in range(7))
    assert manifest.expected_slow_query == "b"
    assert not re.search(r"\b(?:PROFILE|EXPLAIN)\b", case.fast_query + case.slow_query, re.I)
    assert case.fast_query.replace(
        "MATCH (n0:L3)-[r4:T3]->(n6)<-[r5:T3]-(n7:L4), (n5)",
        "MATCH (n5), (n0:L3)-[r4:T3]->(n6)<-[r5:T3]-(n7:L4)",
    ) == case.slow_query


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("case_id", "forged"),
        ("affected_version", "2026.04.1 community"),
        ("image_ref", "neo4j:latest"),
        ("image_digest_env", "GRAPHSYMPERF_NEO4J_BUG001_CONTAINER"),
        ("container_env", "GRAPHSYMPERF_NEO4J_BUG001_IMAGE_DIGEST"),
        ("uri_env", "GRAPHSYMPERF_NEO4J_BUG001_USER"),
        ("user_env", "GRAPHSYMPERF_NEO4J_BUG001_PASSWORD"),
        ("password_env", "GRAPHSYMPERF_NEO4J_BUG001_USER"),
        ("graph_path", "fast_query.cypher"),
        ("graph_path", "../reproducer_graph.cypher.txt"),
        ("graph_sha256", "0" * 64),
        ("fast_query_path", "slow_query.cypher"),
        ("fast_query_sha256", "0" * 64),
        ("slow_query_path", "fast_query.cypher"),
        ("slow_query_sha256", "0" * 64),
        ("expected_slow_query", "a"),
    ),
)
def test_strict_manifest_rejects_identity_path_direction_and_env_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    path = _copy_fixture(tmp_path)
    _mutate_manifest(path, **{field: value})

    with pytest.raises(RegressionConfigurationError, match=field):
        RegressionManifest.load(path)


def test_strict_manifest_rejects_unknown_keys_and_symlink_drift(tmp_path: Path) -> None:
    path = _copy_fixture(tmp_path)
    _mutate_manifest(path, surprise="forged")
    with pytest.raises(RegressionConfigurationError, match="unknown"):
        RegressionManifest.load(path)

    link = tmp_path / "manifest-link.yaml"
    link.symlink_to(path)
    with pytest.raises(RegressionConfigurationError, match="symlink"):
        RegressionManifest.load(link)

    path = _copy_fixture(tmp_path / "second")
    graph = path.parent / "reproducer_graph.cypher.txt"
    target = tmp_path / "graph-copy.cypher.txt"
    target.write_bytes(graph.read_bytes())
    graph.unlink()
    graph.symlink_to(target)
    with pytest.raises(RegressionConfigurationError, match="symlink"):
        RegressionManifest.load(path)


def test_strict_manifest_rejects_every_missing_key_and_fixture_content_drift(
    tmp_path: Path,
) -> None:
    original = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    for key in tuple(original):
        path = _copy_fixture(tmp_path / f"missing-{key}")
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        del payload[key]
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        with pytest.raises(RegressionConfigurationError, match="missing"):
            RegressionManifest.load(path)

    for filename in (
        "reproducer_graph.cypher.txt",
        "fast_query.cypher",
        "slow_query.cypher",
    ):
        path = _copy_fixture(tmp_path / f"drift-{filename}")
        fixture = path.parent / filename
        fixture.write_bytes(fixture.read_bytes() + b"\n// drift")
        with pytest.raises(RegressionConfigurationError, match="SHA-256"):
            RegressionManifest.load(path)


def test_swapped_user_password_env_mapping_fails_without_secret_disclosure(
    tmp_path: Path,
) -> None:
    path = _copy_fixture(tmp_path)
    _mutate_manifest(
        path,
        user_env="GRAPHSYMPERF_NEO4J_BUG001_PASSWORD",
        password_env="GRAPHSYMPERF_NEO4J_BUG001_USER",
    )

    with pytest.raises(RegressionConfigurationError) as caught:
        load_regression_case(path, _environment(), provenance_probe=lambda *_args: IMAGE_DIGEST)

    assert PASSWORD not in str(caught.value)
    assert "neo4j-bug001-user" not in str(caught.value)


def test_provenance_probe_receives_exact_container_and_digest_and_mismatch_fails() -> None:
    observed = []

    def probe(environment, target):
        observed.append((dict(environment), target))
        return IMAGE_DIGEST

    case = load_regression_case(FIXTURE, _environment(), provenance_probe=probe)
    assert observed == [
        (
            {
                "GRAPHSYMPERF_NEO4J_IMAGE_DIGEST": IMAGE_DIGEST,
                "GRAPHSYMPERF_NEO4J_CONTAINER": "neo4j-bug001",
            },
            TargetSystem.NEO4J,
        )
    ]
    assert case.provenance.verified_image_digest == IMAGE_DIGEST

    with pytest.raises(RegressionConfigurationError, match="provenance"):
        load_regression_case(FIXTURE, _environment(), provenance_probe=lambda *_args: None)


@pytest.mark.parametrize(
    "overrides",
    (
        {"GRAPHSYMPERF_NEO4J_URI": "bolt://127.0.0.1:17687"},
        {"GRAPHSYMPERF_NEO4J_CONTAINER": "neo4j-bug001"},
    ),
)
def test_regression_endpoint_and_container_must_be_isolated(overrides: dict[str, str]) -> None:
    with pytest.raises(RegressionConfigurationError, match="must differ"):
        load_regression_case(
            FIXTURE,
            _environment(**overrides),
            provenance_probe=lambda *_args: IMAGE_DIGEST,
        )


@pytest.mark.parametrize(
    ("bug_uri", "main_uri"),
    (
        ("bolt://127.0.0.1:17687/bug001", "bolt://127.0.0.1:7687/main"),
        ("bolt://127.0.0.1:7687/bug001", "bolt://127.0.0.1:7687/main"),
    ),
)
def test_regression_uri_rejects_database_paths_and_shared_main_host_port(
    bug_uri: str,
    main_uri: str,
) -> None:
    with pytest.raises(RegressionConfigurationError, match="URI|endpoint"):
        load_regression_case(
            FIXTURE,
            _environment(
                GRAPHSYMPERF_NEO4J_BUG001_URI=bug_uri,
                GRAPHSYMPERF_NEO4J_URI=main_uri,
            ),
            provenance_probe=lambda *_args: IMAGE_DIGEST,
        )


def test_credentials_never_enter_case_repr_or_gate_report(tmp_path: Path) -> None:
    case = _case()
    path = tmp_path / "regression_report.json"
    report = _runner(case, path).run(case, campaign_identity_hash=_campaign(case))
    serialized = path.read_text(encoding="utf-8")

    assert report.passed is True
    assert PASSWORD not in repr(case)
    assert "neo4j-bug001-user" not in repr(case)
    assert PASSWORD not in serialized
    assert "neo4j-bug001-user" not in serialized


def test_typed_complete_issue_report_is_the_only_pass_and_seals_identities(tmp_path: Path) -> None:
    case = _case()
    path = tmp_path / "regression_report.json"
    report = _runner(case, path).run(case, campaign_identity_hash=_campaign(case))

    assert report.passed is True
    assert report.failure_kind is None
    assert re.fullmatch(r"[0-9a-f]{64}", report.candidate_hash or "")
    assert re.fullmatch(r"[0-9a-f]{64}", report.validation_report_hash or "")
    assert re.fullmatch(r"[0-9a-f]{64}", report.policy_hash or "")
    assert re.fullmatch(r"[0-9a-f]{64}", report.certificate_hash or "")
    assert load_verified_regression_report(
        path,
        case=case,
        campaign_identity_hash=_campaign(case),
        current_identity=_identity(case),
    ) == report


def test_stage_only_namespace_and_empty_sessions_cannot_forge_pass(tmp_path: Path) -> None:
    case = _case()
    namespace = SimpleNamespace(stage="validation_passed", sessions=(), reason_codes=())
    report = _runner(
        case,
        tmp_path / "namespace.json",
        validator=SimpleNamespace(validate=lambda *_args: namespace),
    ).run(case, campaign_identity_hash=_campaign(case))
    assert report.passed is False
    assert report.failure_kind is RegressionFailureKind.INFRASTRUCTURE

    empty = FixedReportValidator(
        lambda report, _candidate: _tampered_report(report, sessions=())
    )
    report = _runner(case, tmp_path / "empty.json", validator=empty).run(
        case, campaign_identity_hash=_campaign(case)
    )
    assert report.passed is False


@pytest.mark.parametrize("identity_kind", ("candidate", "policy", "certificate"))
def test_wrong_candidate_policy_or_certificate_identity_is_rejected(
    tmp_path: Path,
    identity_kind: str,
) -> None:
    case = _case()

    class WrongIdentityValidator:
        def validate(self, candidate, _session_runner):
            policy = candidate.validation_policy
            certificate = candidate.certificate
            config = dict(candidate.resolved_config)
            if identity_kind == "candidate":
                config["forged"] = True
            elif identity_kind == "policy":
                policy = replace(policy, min_ratio=policy.min_ratio + 1.0)
            else:
                certificate = replace(certificate, capability_hash="forged-certificate")
            other = CandidateBundle.create(
                candidate.pair,
                graph_snapshot=candidate.graph_snapshot,
                dbms_version=candidate.dbms_version,
                resolved_config=config,
                discovery_slow_query=candidate.discovery_slow_query,
                discovery_ratio=candidate.discovery_ratio,
                validation_policy=policy,
                certificate=certificate,
            )
            return IssueValidator().validate(other, FakeIndependentRunner())

    report = _runner(
        case,
        tmp_path / f"{identity_kind}.json",
        validator=WrongIdentityValidator(),
    ).run(case, campaign_identity_hash=_campaign(case))

    assert report.passed is False
    assert "validation_identity_mismatch" in report.reason_codes


def test_same_pid_incomplete_equivalence_and_nan_or_inf_are_rejected(tmp_path: Path) -> None:
    case = _case()
    same_pid = FixedReportValidator(
        lambda report, _candidate: _tampered_report(
            report,
            sessions=(report.sessions[0], replace(report.sessions[1], process_id=report.sessions[0].process_id)),
        )
    )
    incomplete_equivalence = FixedReportValidator(
        lambda report, _candidate: _tampered_report(
            report, equivalence=EquivalenceEvidence(True, False, ("result_truncated",))
        )
    )
    for name, validator in (("same-pid", same_pid), ("equivalence", incomplete_equivalence)):
        report = _runner(case, tmp_path / f"{name}.json", validator=validator).run(
            case, campaign_identity_hash=_campaign(case)
        )
        assert report.passed is False

    for value in (float("nan"), float("inf"), float("-inf")):
        validator = FixedReportValidator(
            lambda report, _candidate, value=value: _tampered_report(
                report, validated_ratio=value
            )
        )
        path = tmp_path / f"nonfinite-{repr(value)}.json"
        report = _runner(case, path, validator=validator).run(
            case, campaign_identity_hash=_campaign(case)
        )
        assert report.passed is False
        assert "NaN" not in path.read_text(encoding="utf-8")
        assert "Infinity" not in path.read_text(encoding="utf-8")


def test_version_failure_is_typed_and_observed_version_is_retained(tmp_path: Path) -> None:
    case = _case()
    runner = RegressionRunner._for_test(
        IssueValidator(),
        report_path=tmp_path / "version.json",
        identity_probe=lambda _case: _identity(case, version="2026.04.1 community"),
        session_runner_factory=lambda _case: FakeIndependentRunner(),
    )

    report = runner.run(case, campaign_identity_hash=_campaign(case))

    assert report.failure_kind is RegressionFailureKind.VERSION
    assert report.observed_version == "2026.04.1 community"


def test_failure_taxonomy_uses_typed_evidence_not_reason_substrings(tmp_path: Path) -> None:
    case = _case()

    def graph_failure(report, _candidate):
        lifecycle = replace(report.sessions[0].lifecycle, graph_verified=False)
        session = replace(report.sessions[0], lifecycle=lifecycle)
        return _tampered_report(report, sessions=(session, report.sessions[1]))

    def equivalence_failure(report, _candidate):
        return _tampered_report(
            report,
            equivalence=EquivalenceEvidence(False, True, ("typed_mismatch",)),
        )

    def runtime_failure(report, _candidate):
        return _tampered_report(
            report,
            stage=PipelineStage.REJECTED,
            reason_codes=("ratio_below_threshold",),
        )

    def infrastructure_failure(report, _candidate):
        session = replace(
            report.sessions[1], process_id=report.sessions[0].process_id
        )
        return _tampered_report(report, sessions=(report.sessions[0], session))

    expected = {
        "graph": (graph_failure, RegressionFailureKind.GRAPH),
        "equivalence": (equivalence_failure, RegressionFailureKind.EQUIVALENCE),
        "runtime": (runtime_failure, RegressionFailureKind.RUNTIME),
        "infrastructure": (infrastructure_failure, RegressionFailureKind.INFRASTRUCTURE),
    }
    for name, (transform, kind) in expected.items():
        report = _runner(
            case,
            tmp_path / f"{name}.json",
            validator=FixedReportValidator(transform),
        ).run(case, campaign_identity_hash=_campaign(case))
        assert report.failure_kind is kind


@pytest.mark.parametrize(
    "reason_code",
    (
        "sessions_not_independent",
        "session_fingerprint_or_health_failure",
        "invalid_measurement_protocol",
        "validation_identity_mismatch",
        "session_lifecycle_incomplete",
    ),
)
def test_top_level_infrastructure_reason_precedes_equivalence_taxonomy(
    tmp_path: Path,
    reason_code: str,
) -> None:
    case = _case()

    def infrastructure_with_equivalence_failure(report, _candidate):
        return _tampered_report(
            report,
            stage=PipelineStage.REJECTED,
            reason_codes=(reason_code,),
            equivalence=EquivalenceEvidence(False, True, ("typed_mismatch",)),
        )

    report = _runner(
        case,
        tmp_path / f"top-level-infrastructure-{reason_code}.json",
        validator=FixedReportValidator(infrastructure_with_equivalence_failure),
    ).run(case, campaign_identity_hash=_campaign(case))

    assert report.failure_kind is RegressionFailureKind.INFRASTRUCTURE


def test_fresh_session_version_drift_is_version_typed_and_reports_observed_version(
    tmp_path: Path,
) -> None:
    case = _case()

    def session_version_drift(report, _candidate):
        evidence = replace(
            report.sessions[1].evidence,
            reason_codes=(
                "regression_affected_version_mismatch|observed=2026.04.1 community",
            ),
        )
        session = replace(report.sessions[1], evidence=evidence)
        return _tampered_report(
            report,
            stage=PipelineStage.REJECTED,
            sessions=(report.sessions[0], session),
            reason_codes=("session_validation_failed",),
        )

    report = _runner(
        case,
        tmp_path / "session-version.json",
        validator=FixedReportValidator(session_version_drift),
    ).run(case, campaign_identity_hash=_campaign(case))

    assert report.failure_kind is RegressionFailureKind.VERSION
    assert report.observed_version == "2026.04.1 community"


class _IndexDelegate:
    def __init__(self, states: dict[str, str], *, fail_show: bool = False) -> None:
        self.states = states
        self.fail_show = fail_show
        self.executed: list[tuple[str, str]] = []

    def execute(self, query, _parameters, query_kind, **_kwargs):
        self.executed.append((query, query_kind))
        if query_kind == "regression_index_status":
            if self.fail_show:
                raise RuntimeError("driver-timeout")
            return SimpleNamespace(
                rows=tuple({"name": name, "state": state} for name, state in self.states.items())
            )
        return SimpleNamespace(rows=())

    def load_gdsmith_graph(self, statements):
        return tuple(statements)


class _VersionDelegate:
    def __init__(self) -> None:
        self.query = ""

    def connectivity_check(self):
        return True, "connected"

    def execute(self, query, _parameters, query_kind, **_kwargs):
        assert query_kind == "regression_version_identity"
        self.query = query
        return SimpleNamespace(rows=({"version": "2026.04.0", "edition": "community"},))


def test_version_identity_filters_non_dbms_component_rows() -> None:
    delegate = _VersionDelegate()
    adapter = _AffectedVersionNeo4jAdapter(delegate, _case())

    assert adapter.connectivity_check() == (True, "connected")
    assert "trim(edition) <> ''" in delegate.query
    assert adapter.observed_version == "2026.04.0 community"


def test_each_session_awaits_and_verifies_exactly_seven_online_indexes() -> None:
    case = _case()
    delegate = _IndexDelegate({name: "ONLINE" for name in case.index_names})
    adapter = _AffectedVersionNeo4jAdapter(delegate, case)

    adapter.load_gdsmith_graph(case.graph_snapshot.statements)

    kinds = [kind for _query, kind in delegate.executed]
    assert kinds.count("regression_graph_index") == 7
    assert kinds[-2:] == ["regression_index_await", "regression_index_status"]
    assert adapter.verified_index_states == ("ONLINE",) * 7
    assert not any(re.search(r"\b(?:PROFILE|EXPLAIN)\b", query, re.I) for query, _ in delegate.executed)


def test_missing_or_nononline_index_is_graph_failure_but_show_error_remains_infrastructure() -> None:
    case = _case()
    for states in (
        {name: "ONLINE" for name in case.index_names[:-1]},
        {name: ("POPULATING" if name == "i3" else "ONLINE") for name in case.index_names},
    ):
        with pytest.raises(RegressionGraphStateError, match="index_state"):
            _AffectedVersionNeo4jAdapter(_IndexDelegate(states), case).load_gdsmith_graph(
                case.graph_snapshot.statements
            )
    with pytest.raises(RuntimeError, match="driver-timeout"):
        _AffectedVersionNeo4jAdapter(
            _IndexDelegate({name: "ONLINE" for name in case.index_names}, fail_show=True), case
        ).load_gdsmith_graph(case.graph_snapshot.statements)


def test_production_constructor_is_fixed_and_session_builder_is_spawned(tmp_path: Path) -> None:
    case = _case()
    with pytest.raises(TypeError):
        RegressionRunner(IssueValidator(), report_path=tmp_path / "forged.json")  # type: ignore[misc]
    assert isinstance(build_independent_session_runner(case), SpawnedValidationSessionRunner)


def test_known_regression_session_budget_covers_the_fixed_strict_protocol() -> None:
    candidate = regression_module._candidate(_case(), _identity(_case()))

    assert candidate.validation_policy.session_timeout_seconds == 1_200.0


def test_landed_report_rejects_campaign_manifest_environment_and_content_drift(tmp_path: Path) -> None:
    case = _case()
    path = tmp_path / "report.json"
    _runner(case, path).run(case, campaign_identity_hash=_campaign(case))

    with pytest.raises(RegressionArtifactError, match="identity"):
        load_verified_regression_report(
            path,
            case=case,
            campaign_identity_hash="c" * 64,
            current_identity=_identity(case),
        )

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["passed"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegressionArtifactError, match="hash"):
        load_verified_regression_report(
            path,
            case=case,
            campaign_identity_hash=_campaign(case),
            current_identity=_identity(case),
        )


def test_landed_report_rejects_coherently_rehashed_validation_identity_forgery(
    tmp_path: Path,
) -> None:
    case = _case()
    path = tmp_path / "report.json"
    _runner(case, path).run(case, campaign_identity_hash=_campaign(case))
    payload = json.loads(path.read_text(encoding="utf-8"))
    for index, name in enumerate(
        ("candidate_hash", "validation_report_hash", "policy_hash", "certificate_hash"),
        start=1,
    ):
        payload[name] = f"{index:x}" * 64
    payload_without_hash = {name: value for name, value in payload.items() if name != "content_hash"}
    payload["content_hash"] = canonical_content_hash(payload_without_hash)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegressionArtifactError, match="evidence|identity|hash"):
        load_verified_regression_report(
            path,
            case=case,
            campaign_identity_hash=_campaign(case),
            current_identity=_identity(case),
        )


@pytest.mark.parametrize("field", ("fast_query_sha256", "slow_query_sha256"))
def test_landed_report_rejects_coherently_rehashed_query_summary_drift(
    tmp_path: Path,
    field: str,
) -> None:
    case = _case()
    report_path = tmp_path / "report.json"
    _runner(case, report_path).run(case, campaign_identity_hash=_campaign(case))
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload[field] = "0" * 64
    body = {name: value for name, value in payload.items() if name != "content_hash"}
    payload["content_hash"] = canonical_content_hash(body)
    report_path.write_bytes(_canonical_json_bytes(payload))

    with pytest.raises(RegressionArtifactError, match="identity"):
        load_verified_regression_report(
            report_path,
            case=case,
            campaign_identity_hash=_campaign(case),
            current_identity=_identity(case),
        )


@pytest.mark.parametrize(
    "tamper",
    ("missing", "symlink", "unknown_key", "content_hash", "file_sha"),
)
def test_landed_report_rejects_sidecar_topology_schema_and_hash_drift(
    tmp_path: Path,
    tamper: str,
) -> None:
    case = _case()
    report_path = tmp_path / "report.json"
    _runner(case, report_path).run(case, campaign_identity_hash=_campaign(case))
    sidecar_path = regression_validation_evidence_path(report_path)
    if tamper == "missing":
        sidecar_path.unlink()
    elif tamper == "symlink":
        target = tmp_path / "sidecar-target.json"
        sidecar_path.replace(target)
        sidecar_path.symlink_to(target)
    elif tamper == "unknown_key":
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        payload["unknown"] = True
        _rebind_sidecar_and_main(report_path, payload)
    elif tamper == "content_hash":
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        payload["content_hash"] = "0" * 64
        _rebind_sidecar_and_main(report_path, payload, rehash_sidecar=False)
    else:
        sidecar_path.write_bytes(sidecar_path.read_bytes() + b"\n")

    with pytest.raises(RegressionArtifactError, match="evidence"):
        load_verified_regression_report(
            report_path,
            case=case,
            campaign_identity_hash=_campaign(case),
            current_identity=_identity(case),
        )


@pytest.mark.parametrize(
    "tamper",
    ("query", "candidate", "policy", "certificate", "session"),
)
def test_landed_report_rejects_coherently_rebound_raw_validation_drift(
    tmp_path: Path,
    tamper: str,
) -> None:
    case = _case()
    report_path = tmp_path / "report.json"
    _runner(case, report_path).run(case, campaign_identity_hash=_campaign(case))
    sidecar_path = regression_validation_evidence_path(report_path)
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    raw_report = payload["report"]
    if tamper == "query":
        raw_report["manifest"]["pair"]["query_a"] += " "
    elif tamper == "candidate":
        raw_report["manifest"]["candidate_hash"] = "f" * 64
    elif tamper == "policy":
        raw_report["validation_policy"]["min_ratio"] = 99.0
    elif tamper == "certificate":
        raw_report["manifest"]["certificate"]["capability_hash"] = "forged"
    else:
        raw_report["sessions"][0]["schedule_seed"] += 1
    _rebind_sidecar_and_main(report_path, payload)

    with pytest.raises(RegressionArtifactError, match="evidence"):
        load_verified_regression_report(
            report_path,
            case=case,
            campaign_identity_hash=_campaign(case),
            current_identity=_identity(case),
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"observed_version": "2026.04.1 community"},
        {"driver_version": "6.0.1"},
        {"dbms_fingerprint": "c" * 64},
        {"index_states": ("ONLINE",) * 6 + ("POPULATING",)},
    ),
)
def test_landed_report_rejects_live_environment_identity_drift(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    case = _case()
    report_path = tmp_path / "report.json"
    _runner(case, report_path).run(case, campaign_identity_hash=_campaign(case))

    with pytest.raises(RegressionArtifactError, match="identity"):
        load_verified_regression_report(
            report_path,
            case=case,
            campaign_identity_hash=_campaign(case),
            current_identity=replace(_identity(case), **changes),
        )


def test_atomic_report_creation_fsyncs_each_new_directory_and_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "new-parent" / "new-child" / "artifact.json"
    real_open = regression_module.os.open
    opened_directories: list[Path] = []

    def recording_open(path, flags, *args, **kwargs):
        candidate = Path(path)
        if candidate.is_dir():
            opened_directories.append(candidate.resolve())
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(regression_module.os, "open", recording_open)
    regression_module._atomic_write_payload(destination, {"value": 1})

    assert {
        tmp_path.resolve(),
        (tmp_path / "new-parent").resolve(),
        (tmp_path / "new-parent" / "new-child").resolve(),
    } <= set(opened_directories)
