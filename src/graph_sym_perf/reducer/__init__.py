"""Dependency-aware runtime-preserving reducer helpers."""


def reduce_candidate(*args, **kwargs):
    """Legacy compatibility shim; new code uses ``CandidateReducer`` directly."""
    from graph_sym_perf.reducer.candidate import reduce_candidate as legacy_reduce_candidate

    return legacy_reduce_candidate(*args, **kwargs)


__all__ = ["reduce_candidate"]
