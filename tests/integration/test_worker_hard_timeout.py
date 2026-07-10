from graph_sym_perf.experiment.process_control import terminate_process_group


def test_hard_timeout_terminates_the_entire_process_group(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("graph_sym_perf.experiment.process_control.os.getpgid", lambda pid: pid + 1)
    monkeypatch.setattr("graph_sym_perf.experiment.process_control.os.killpg", lambda pgid, signal: calls.append((pgid, signal)))
    terminate_process_group(41)
    assert calls and calls[0][0] == 42
