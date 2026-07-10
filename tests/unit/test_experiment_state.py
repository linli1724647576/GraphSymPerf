from graph_sym_perf.experiment.state import ShardState, can_transition


def test_shard_state_machine_allows_progress_and_terminal_incomplete() -> None:
    assert can_transition(ShardState.PLANNED, ShardState.GENERATING)
    assert can_transition(ShardState.BUILDING_ACCEPTED_PAIRS, ShardState.INCOMPLETE)
    assert not can_transition(ShardState.COMPLETED, ShardState.GENERATING)
