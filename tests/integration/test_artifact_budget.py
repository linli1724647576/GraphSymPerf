import pytest

from graph_sym_perf.artifacts.store import ContentAddressedStore


def test_store_enforces_per_shard_budget(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path, budget_bytes=3)
    store.put("a", b"abc")
    with pytest.raises(ValueError):
        store.put("b", b"d")
