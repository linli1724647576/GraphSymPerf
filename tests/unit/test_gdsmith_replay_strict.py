from __future__ import annotations

import pytest

from graph_sym_perf.gdsmith.replay import parse_gdsmith_replay_graph


def test_neutral_graph_lowering_rejects_unconsumed_statements() -> None:
    with pytest.raises(ValueError, match="UNSUPPORTED_GDSMITH_GRAPH_STATEMENT"):
        parse_gdsmith_replay_graph(("CREATE INDEX ON :A(id)",))
