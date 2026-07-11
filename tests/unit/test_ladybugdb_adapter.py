from __future__ import annotations

from types import SimpleNamespace

import pytest

from graph_sym_perf.adapters.ladybugdb import LadybugDBAdapter
from graph_sym_perf.core import AdapterError


def test_ladybugdb_reset_removes_the_dedicated_database_file(tmp_path) -> None:
    database_path = tmp_path / "campaign.lbug"
    database_path.write_bytes(b"stale schema")
    closed = []
    adapter = object.__new__(LadybugDBAdapter)
    adapter.env = {"GRAPHSYMPERF_LADYBUGDB_DATABASE_PATH": str(database_path)}
    adapter._conn = SimpleNamespace(close=lambda: closed.append(True))
    adapter._db = object()

    adapter.reset_schema()

    assert closed == [True]
    assert adapter._conn is None
    assert adapter._db is None
    assert not database_path.exists()


def test_ladybugdb_reset_refuses_to_delete_a_directory(tmp_path) -> None:
    adapter = object.__new__(LadybugDBAdapter)
    adapter.env = {"GRAPHSYMPERF_LADYBUGDB_DATABASE_PATH": str(tmp_path)}
    adapter._conn = None
    adapter._db = None

    with pytest.raises(AdapterError, match="regular database file"):
        adapter.reset_schema()

    assert tmp_path.exists()
