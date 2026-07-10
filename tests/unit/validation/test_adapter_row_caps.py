from graph_sym_perf.adapters.falkordb import _bounded_read_query
from graph_sym_perf.adapters.ladybugdb import _ladybugdb_rows_bounded


class StreamingResult:
    def __init__(self, count: int) -> None:
        self._rows = iter(([index] for index in range(count)))
        self._next = None
        self.get_next_calls = 0

    def get_column_names(self):
        return ["value"]

    def has_next(self):
        if self._next is None:
            try:
                self._next = next(self._rows)
            except StopIteration:
                return False
        return True

    def get_next(self):
        self.get_next_calls += 1
        value = self._next
        self._next = None
        return value


def test_falkordb_query_is_server_bounded_before_materialization() -> None:
    assert _bounded_read_query("MATCH (n) RETURN n", 100_000).endswith("LIMIT 100001")
    assert _bounded_read_query("MATCH (n) RETURN n LIMIT 200000", 100_000).endswith("LIMIT 100001")


def test_ladybugdb_streaming_stops_after_cap_plus_one() -> None:
    result = StreamingResult(1_000)

    rows, truncated = _ladybugdb_rows_bounded(result, max_rows=2)

    assert len(rows) == 3
    assert truncated
    assert result.get_next_calls == 3
