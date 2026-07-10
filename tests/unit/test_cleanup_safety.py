from graph_sym_perf.workspace_cleanup import audit_paths


def test_audit_marks_uncommitted_tracked_paths_protected(tmp_path, monkeypatch) -> None:
    path = tmp_path / "tracked.txt"
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr("graph_sym_perf.workspace_cleanup._git_state", lambda item: (True, True))
    manifest = audit_paths((path,))
    assert manifest[0]["status"] == "PROTECTED"
