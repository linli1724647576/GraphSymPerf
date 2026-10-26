import pytest

from executor import validate_read_query


def test_generated_global_limit_is_accepted_as_a_read_query():
    validate_read_query("CALL () { RETURN 1 AS c UNION ALL RETURN 1 AS c } RETURN c LIMIT 2")


def test_cypher_25_prefix_allows_a_native_when_inside_a_scalar_consumer():
    validate_read_query("CYPHER 25 MATCH (u) RETURN COUNT { WHEN true THEN { RETURN 1 AS c } ELSE { RETURN 2 AS c } } AS n")


@pytest.mark.parametrize("query", [
    "CALL db.labels() YIELD label RETURN label",
    "CALL () { CREATE (n) RETURN n AS c } RETURN c LIMIT 2",
    "CALL () { RETURN 1 AS c UNION ALL RETURN 1 AS c } RETURN c LIMIT 2 DELETE c",
    "CYPHER 25 WHEN true THEN { RETURN 1 AS c } ELSE { RETURN 2 AS c } DELETE c",
    "CYPHER 25 MATCH (u) DELETE u RETURN 1 AS c",
])
def test_procedures_and_writes_do_not_gain_access_through_the_row_stream_gate(query):
    with pytest.raises(ValueError):
        validate_read_query(query)
