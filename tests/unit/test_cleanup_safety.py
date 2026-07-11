import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from graph_sym_perf.workspace_cleanup import audit_paths, delete_approved, discover_cleanup_paths
from scripts import audit_cleanup


def _audit_item(path: str, *, status: str = "APPROVED") -> dict[str, object]:
    return {
        "path": path,
        "bytes": 0,
        "file_count": 0,
        "tracked": False,
        "uncommitted": False,
        "evidence_references": [],
        "migration_target": None,
        "deletion_reason": "test_generated_artifact",
        "status": status,
    }


def _final_manifest(root: Path, items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "dry_run": False,
        "root": str(root.resolve()),
        "items": items,
    }


def test_audit_records_required_fields_and_protects_uncommitted_tracked_paths(tmp_path, monkeypatch) -> None:
    path = tmp_path / "results"
    path.mkdir()
    (path / "runtime.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("graph_sym_perf.workspace_cleanup._git_state", lambda item, root: (True, True))

    manifest = audit_paths(
        (path,),
        root=tmp_path,
        migrations={
            "results": {
                "evidence_references": ["issues/evidence_inventory.json"],
                "migration_target": "issues/evidence/",
                "deletion_reason": "migrated_historical_result",
            }
        },
    )

    assert manifest == (
        {
            "path": "results",
            "bytes": 3,
            "file_count": 1,
            "tracked": True,
            "uncommitted": True,
            "evidence_references": ["issues/evidence_inventory.json"],
            "migration_target": "issues/evidence/",
            "deletion_reason": "uncommitted_tracked_path",
            "status": "PROTECTED",
        },
    )


def test_discovery_is_fixed_scope_and_never_includes_protected_roots(tmp_path) -> None:
    expected = (
        tmp_path / ".pytest_cache",
        tmp_path / "BUG list",
        tmp_path / "doctor.json",
        tmp_path / "scratch" / "runtime-sample.csv",
        tmp_path / "scratch" / "tmp_plot.pdf",
        tmp_path / "src" / "pkg" / "__pycache__",
    )
    for path in expected:
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}" if path.suffix == ".json" else "fixture", encoding="utf-8")
        else:
            path.mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__pycache__" / "module.pyc").write_bytes(b"cache")
    (tmp_path / ".venv" / "cache").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "__pycache__").mkdir(parents=True)
    (tmp_path / "issues" / "evidence" / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "tests" / "fixtures" / "regressions" / "__pycache__").mkdir(parents=True)
    (tmp_path / "docs" / "paper.pdf").parent.mkdir()
    (tmp_path / "docs" / "paper.pdf").write_bytes(b"pdf")

    assert discover_cleanup_paths(tmp_path) == expected


def test_delete_requires_final_manifest_and_only_consumes_explicit_approved_items(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "src" / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    candidate = tmp_path / "doctor.json"
    candidate.write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "deletion_manifest.json"
    removed: list[Path] = []
    monkeypatch.setattr("graph_sym_perf.workspace_cleanup._git_state", lambda item, root: (False, False))
    monkeypatch.setattr(
        "graph_sym_perf.workspace_cleanup._delete_exact",
        lambda root, relative: removed.append(root / relative),
    )

    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dry_run": False,
                "root": str(tmp_path.resolve()),
                "items": [
                    _audit_item("src/pkg/__pycache__"),
                    _audit_item("doctor.json", status="CANDIDATE"),
                ],
            }
        ),
        encoding="utf-8",
    )

    deleted = delete_approved(manifest_path, root=tmp_path)

    assert deleted == ({"path": "src/pkg/__pycache__", "bytes": 0, "file_count": 0, "status": "DELETED"},)
    assert removed == [cache]

    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dry_run": True,
                "root": str(tmp_path.resolve()),
                "items": [_audit_item("doctor.json")],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="final approved deletion manifest"):
        delete_approved(manifest_path, root=tmp_path)


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "../outside",
        ".git",
        ".git/objects",
        ".venv",
        "issues",
        "issues/evidence",
        "tests/fixtures",
        "tests/fixtures/regressions",
    ),
)
def test_delete_rejects_escaping_or_protected_approved_paths(tmp_path, unsafe_path) -> None:
    manifest_path = tmp_path / "deletion_manifest.json"
    manifest_path.write_text(
        json.dumps(_final_manifest(tmp_path, [_audit_item(unsafe_path)])),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe|protected|scope"):
        delete_approved(manifest_path, root=tmp_path)


def test_delete_rejects_symlink_and_runtime_dirty_tracked_path(tmp_path, monkeypatch) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    symlink = tmp_path / "results"
    symlink.symlink_to(outside, target_is_directory=True)
    manifest_path = tmp_path / "deletion_manifest.json"
    manifest_path.write_text(
        json.dumps(_final_manifest(tmp_path, [_audit_item("results")])),
        encoding="utf-8",
    )
    monkeypatch.setattr("graph_sym_perf.workspace_cleanup._git_state", lambda item, root: (False, False))
    with pytest.raises(ValueError, match="symlink"):
        delete_approved(manifest_path, root=tmp_path)

    symlink.unlink()
    symlink.mkdir()
    monkeypatch.setattr("graph_sym_perf.workspace_cleanup._git_state", lambda item, root: (True, True))
    with pytest.raises(ValueError, match="tracked-uncommitted"):
        delete_approved(manifest_path, root=tmp_path)
    assert symlink.is_dir()


def test_cli_has_no_command_that_marks_candidates_approved(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        audit_cleanup.main(["--approve"])
    assert error.value.code == 2
    assert "--approve" not in capsys.readouterr().err.splitlines()[0]


def test_cli_default_is_dry_run_and_never_writes_approved_status(tmp_path, monkeypatch) -> None:
    (tmp_path / "doctor.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("graph_sym_perf.workspace_cleanup._git_state", lambda item, root: (False, False))

    assert audit_cleanup.main(["--root", str(tmp_path)]) == 0

    payload = json.loads((tmp_path / "deletion_manifest.json").read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["items"][0]["status"] == "CANDIDATE"
    assert all(item["status"] != "APPROVED" for item in payload["items"])


def test_cli_execute_refuses_unverified_evidence_boundary(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "deletion_manifest.json"
    manifest.write_text(
        json.dumps(_final_manifest(tmp_path, [])),
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_cleanup, "delete_approved", lambda *args, **kwargs: pytest.fail("delete called"))

    with pytest.raises(ValueError, match="evidence inventory"):
        audit_cleanup.main(["--root", str(tmp_path), "--execute-approved"])


def test_historical_evidence_inventory_is_verified_but_not_promoted_to_regressions() -> None:
    inventory = json.loads(Path("issues/evidence_inventory.json").read_text(encoding="utf-8"))
    assert inventory["schema_version"] == 3
    assert len(inventory["packages"]) == 11
    assert all(record["audit_status"] == "VERIFIED" for record in inventory["packages"])
    assert all(record["hashes_valid"] is True for record in inventory["packages"])
    assert all(record["metadata_schema_valid"] is True for record in inventory["packages"])
    assert all(record["github_url_valid"] is True for record in inventory["packages"])
    assert all(record["reproducer_status"] == "AVAILABLE_HISTORICAL" for record in inventory["packages"])

    regressions = yaml.safe_load(Path("tests/fixtures/regressions/manifest.yaml").read_text(encoding="utf-8"))
    assert regressions["cases"] == []
    assert len(regressions["excluded_evidence_packages"]) == 11
    assert {
        item["reason"] for item in regressions["excluded_evidence_packages"]
    } == {"historical_evidence_not_v3_executable"}


def test_delete_rejects_manifest_bound_to_a_different_root(tmp_path, monkeypatch) -> None:
    approved_root = tmp_path / "approved-root"
    execution_root = tmp_path / "execution-root"
    approved_root.mkdir()
    execution_root.mkdir()
    target = execution_root / "doctor.json"
    target.write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "deletion_manifest.json"
    manifest_path.write_text(
        json.dumps(_final_manifest(approved_root, [_audit_item("doctor.json")])),
        encoding="utf-8",
    )
    monkeypatch.setattr("graph_sym_perf.workspace_cleanup._git_state", lambda item, root: (False, False))

    with pytest.raises(ValueError, match="root"):
        delete_approved(manifest_path, root=execution_root)

    assert target.is_file()


def test_delete_revalidates_all_paths_after_symlink_swap_before_any_deletion(tmp_path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    doctor = root / "doctor.json"
    doctor.write_text("{}", encoding="utf-8")
    results = root / "results"
    cache = results / "cache"
    cache.mkdir(parents=True)
    (cache / "inside.txt").write_text("inside", encoding="utf-8")
    outside_cache = tmp_path / "outside" / "cache"
    outside_cache.mkdir(parents=True)
    (outside_cache / "outside.txt").write_text("outside", encoding="utf-8")
    original_results = root / "original-results"
    manifest_path = root / "deletion_manifest.json"
    manifest_path.write_text(
        json.dumps(
            _final_manifest(
                root,
                [_audit_item("doctor.json"), _audit_item("results/cache")],
            )
        ),
        encoding="utf-8",
    )
    swapped = False

    def swap_parent_after_validation(path: Path, audit_root: Path) -> tuple[bool, bool]:
        nonlocal swapped
        if path == cache and not swapped:
            results.rename(original_results)
            results.symlink_to(tmp_path / "outside", target_is_directory=True)
            swapped = True
        return False, False

    monkeypatch.setattr("graph_sym_perf.workspace_cleanup._git_state", swap_parent_after_validation)

    with pytest.raises(ValueError, match="symlink|changed"):
        delete_approved(manifest_path, root=root)

    assert doctor.is_file()
    assert (original_results / "cache" / "inside.txt").is_file()
    assert (outside_cache / "outside.txt").is_file()


def test_delete_accepts_temp_files_emitted_by_discovery(tmp_path, monkeypatch) -> None:
    target = tmp_path / "scratch" / "runtime-sample.csv"
    target.parent.mkdir()
    target.write_text("runtime\n", encoding="utf-8")
    manifest_path = tmp_path / "deletion_manifest.json"
    manifest_path.write_text(
        json.dumps(_final_manifest(tmp_path, [_audit_item("scratch/runtime-sample.csv")])),
        encoding="utf-8",
    )
    monkeypatch.setattr("graph_sym_perf.workspace_cleanup._git_state", lambda item, root: (False, False))

    deleted = delete_approved(manifest_path, root=tmp_path)

    assert deleted[0]["path"] == "scratch/runtime-sample.csv"
    assert not target.exists()


def test_descriptor_anchored_delete_rejects_parent_swap_after_final_check(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "workspace"
    target = root / "results" / "cache"
    target.mkdir(parents=True)
    (target / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside_cache = outside / "cache"
    outside_cache.mkdir(parents=True)
    (outside_cache / "outside.txt").write_text("outside", encoding="utf-8")
    manifest_path = root / "deletion_manifest.json"
    manifest_path.write_text(
        json.dumps(_final_manifest(root, [_audit_item("results/cache")])),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "graph_sym_perf.workspace_cleanup._git_state",
        lambda item, audit_root: (False, False),
    )
    from graph_sym_perf import workspace_cleanup

    original_validate = workspace_cleanup._validate_approved_path
    calls = 0

    def swap_after_final_validation(relative: Path, audit_root: Path) -> None:
        nonlocal calls
        original_validate(relative, audit_root)
        calls += 1
        if calls == 3:
            (root / "results").rename(root / "original-results")
            (root / "results").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        "graph_sym_perf.workspace_cleanup._validate_approved_path",
        swap_after_final_validation,
    )

    with pytest.raises(OSError):
        delete_approved(manifest_path, root=root)

    assert (root / "original-results" / "cache" / "inside.txt").is_file()
    assert (outside_cache / "outside.txt").is_file()


def test_delete_rejects_manifest_items_missing_audit_fields(tmp_path) -> None:
    manifest_path = tmp_path / "deletion_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dry_run": False,
                "root": str(tmp_path.resolve()),
                "items": [{"path": "doctor.json", "status": "APPROVED"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="audit fields"):
        delete_approved(manifest_path, root=tmp_path)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("path", 1),
        ("bytes", True),
        ("file_count", "0"),
        ("tracked", 0),
        ("uncommitted", None),
        ("evidence_references", [1]),
        ("migration_target", 1),
        ("deletion_reason", None),
        ("status", ["APPROVED"]),
    ),
)
def test_delete_rejects_invalid_audit_field_types(tmp_path, field, invalid_value) -> None:
    item = _audit_item("doctor.json")
    item[field] = invalid_value
    manifest_path = tmp_path / "deletion_manifest.json"
    manifest_path.write_text(
        json.dumps(_final_manifest(tmp_path, [item])),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest"):
        delete_approved(manifest_path, root=tmp_path)


def test_git_state_failure_is_fail_closed(tmp_path, monkeypatch) -> None:
    target = tmp_path / "results"
    target.mkdir()
    monkeypatch.setattr(
        "graph_sym_perf.workspace_cleanup.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=128, stdout="", stderr="fatal: not a git repository"),
    )

    with pytest.raises(RuntimeError, match="git ls-files"):
        audit_paths((target,), root=tmp_path)
