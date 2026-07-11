from __future__ import annotations

import pytest

from graph_sym_perf.adapters.agensgraph import AgensGraphAdapter
from graph_sym_perf.core import AdapterError


def test_agensgraph_rejects_multilabel_graph_instead_of_selecting_primary_label() -> None:
    adapter = object.__new__(AgensGraphAdapter)
    with pytest.raises(AdapterError, match="MULTI_LABEL_UNSUPPORTED"):
        adapter.load_gdsmith_graph(("CREATE (n:A:B {id: 1})",))
