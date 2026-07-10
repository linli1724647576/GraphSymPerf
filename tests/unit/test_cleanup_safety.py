import json

from graph_sym_perf.workspace_cleanup import approve_candidates, audit_paths, delete_approved


def test_audit_marks_uncommitted_tracked_paths_protected(tmp_path, monkeypatch) -> None:
    path = tmp_path / "tracked.txt"
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr("graph_sym_perf.workspace_cleanup._git_state", lambda item: (True, True))
    manifest = audit_paths((path,))
    assert manifest[0]["status"] == "PROTECTED"


def test_delete_consumes_only_approved_exact_manifest_paths(tmp_path) -> None:
    approved = tmp_path / "runs"
    protected = tmp_path / "keep"
    approved.mkdir()
    protected.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"dry_run": True, "items": [{"path": "runs", "status": "CANDIDATE"}]}), encoding="utf-8")

    approve_candidates(manifest_path)
    deleted = delete_approved(manifest_path, root=tmp_path)

    assert deleted[0]["path"] == "runs"
    assert not approved.exists()
    assert protected.exists()
