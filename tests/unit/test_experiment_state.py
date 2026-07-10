import json
import errno

import pytest

from graph_sym_perf.experiment.state import (
    ErrorKind,
    IllegalTransition,
    ShardJournal,
    ShardState,
    can_transition,
    transition,
)


def test_shard_state_machine_allows_progress_and_terminal_incomplete() -> None:
    assert can_transition(ShardState.PLANNED, ShardState.GENERATING)
    assert can_transition(ShardState.BUILDING_ACCEPTED_PAIRS, ShardState.INCOMPLETE)
    assert not can_transition(ShardState.SCREENING, ShardState.COMPLETED)
    assert can_transition(ShardState.SCREENING, ShardState.VALIDATING)
    assert can_transition(ShardState.VALIDATING, ShardState.REDUCING)
    assert can_transition(ShardState.REDUCING, ShardState.COMPLETED)
    assert not can_transition(ShardState.COMPLETED, ShardState.GENERATING)


def test_error_kind_is_a_closed_typed_reliability_taxonomy() -> None:
    assert {kind.value for kind in ErrorKind} == {
        "QUERY_TIMEOUT",
        "DRIVER_TIMEOUT",
        "WORKER_HUNG",
        "WORKER_CRASHED",
        "DBMS_UNAVAILABLE",
        "DBMS_CRASHED",
        "INFRASTRUCTURE_ERROR",
    }


def test_illegal_transition_is_rejected_instead_of_only_returning_false() -> None:
    with pytest.raises(IllegalTransition):
        transition(ShardState.COMPLETED, ShardState.GENERATING)


def test_shard_journal_appends_typed_events_and_checkpoints_resume_progress(tmp_path) -> None:
    journal = ShardJournal(tmp_path, manifest_hash="manifest-a")

    journal.transition(ShardState.GENERATING, attempt=0)
    journal.update_progress(
        attempt=0,
        accepted_pair_hashes={"pair-b", "pair-a"},
        candidate_hashes={"candidate-a"},
        last_seed=17,
        generator_progress={"generated": 9},
    )

    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert checkpoint == {
        "accepted_pair_hashes": ["pair-a", "pair-b"],
        "attempt": 0,
        "candidate_hashes": ["candidate-a"],
        "event_seq": 1,
        "generator_progress": {"generated": 9},
        "last_seed": 17,
        "manifest_hash": "manifest-a",
        "stage_evidence": {},
        "state": "generating",
    }
    assert events[0]["previous_state"] == "planned"
    assert events[0]["state"] == "generating"
    assert events[0]["seq"] == 1


def test_disk_full_during_checkpoint_replace_preserves_the_previous_atomic_best_state(tmp_path, monkeypatch) -> None:
    journal = ShardJournal(tmp_path, manifest_hash="manifest-a")
    journal.transition(ShardState.GENERATING, attempt=0)
    previous = (tmp_path / "checkpoint.json").read_bytes()

    def disk_full(_source, _destination) -> None:
        raise OSError(errno.ENOSPC, "injected disk full")

    monkeypatch.setattr("graph_sym_perf.experiment.state.os.replace", disk_full)

    with pytest.raises(OSError, match="failed to atomically persist checkpoint"):
        journal.update_progress(attempt=0, accepted_pair_hashes={"uncommitted"})

    assert (tmp_path / "checkpoint.json").read_bytes() == previous
    assert journal.accepted_pair_hashes == frozenset()
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_resume_replays_fsynced_event_tail_when_checkpoint_lags(tmp_path, monkeypatch) -> None:
    journal = ShardJournal(tmp_path, manifest_hash="manifest-a")
    journal.transition(ShardState.GENERATING, attempt=0)
    real_replace = __import__("os").replace

    def disk_full_once(_source, _destination) -> None:
        raise OSError(errno.ENOSPC, "injected disk full after event fsync")

    monkeypatch.setattr("graph_sym_perf.experiment.state.os.replace", disk_full_once)
    with pytest.raises(OSError):
        journal.transition(ShardState.LOADING, attempt=0)
    monkeypatch.setattr("graph_sym_perf.experiment.state.os.replace", real_replace)

    recovered = ShardJournal(tmp_path, manifest_hash="manifest-a")

    assert recovered.state is ShardState.LOADING
    assert recovered.event_seq == 2
