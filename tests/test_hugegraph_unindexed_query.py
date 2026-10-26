"""Preserve native queries and classify only HugeGraph's exact index boundary."""

import json
from types import SimpleNamespace

import pytest

from executor import ApacheHugeGraph, CaseFailure, JanusGraph


MESSAGE = ("Don't accept query based on properties [uid] that are not indexed "
           "in label 'ScorRoot', may not match secondary condition")
QUERY = "g.inject(1).V().hasLabel('ScorRoot').has('uid',P.eq(_gsp_p0)).project('c0').by(__.values('uid'))"


def adapter(kind, code, message):
    db = kind.__new__(kind)
    db.database, db.settings = "gsp_test", {"timeout_seconds": 2.0, "max_rows": 10}
    db._endpoint, db._headers, db._traversal_source, db._graph_binding = "/gremlin", {}, "g", "graph"
    calls = []

    class Response:
        status, reason = code, "native HTTP response"

        def getheader(self, name, default=None):
            return default

        def read(self):
            return json.dumps({"status": {"code": code, "message": message},
                               "result": {"data": [{"c0": 1}] if code == 200 else []}}).encode()

    db._connection = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda timeout: None),
        request=lambda *args, **kwargs: calls.append((args, kwargs)), getresponse=Response)
    return db, calls


@pytest.mark.parametrize("code", [400, 500])
def test_exact_hugegraph_missing_index_message_is_a_per_input_unsupported_boundary(code):
    db, calls = adapter(ApacheHugeGraph, code, MESSAGE)
    with pytest.raises(CaseFailure) as caught:
        db.execute(QUERY, {"_gsp_p0": 1}, 2.0)
    failure = caught.value
    assert failure.status == "unsupported_feature" and failure.stop_target is False
    assert failure.reason == f"ApacheHugeGraph_server_{code}"
    assert failure.details == {"server_message": MESSAGE}
    assert len(calls) == 1
    request = json.loads(calls[0][1]["body"])
    assert request["gremlin"] == f"({QUERY}).limit(11).toList()"
    assert request["bindings"] == {"_gsp_p0": 1}


@pytest.mark.parametrize("kind,message", [
    (JanusGraph, MESSAGE),
    (ApacheHugeGraph, "backend unavailable"),
    (ApacheHugeGraph, MESSAGE.replace("secondary condition", "range condition")),
    (ApacheHugeGraph, MESSAGE.replace("in label 'ScorRoot'", "in any label")),
    (ApacheHugeGraph, "prefix " + MESSAGE),
    (ApacheHugeGraph, MESSAGE + "; another error"),
])
@pytest.mark.parametrize("code", [400, 500])
def test_other_provider_or_other_server_errors_are_not_silently_skipped(kind, message, code):
    db, _ = adapter(kind, code, message)
    with pytest.raises(CaseFailure) as caught:
        db.execute(QUERY, {}, 2.0)
    assert caught.value.status == "query_error" and caught.value.stop_target is False
    assert caught.value.reason == f"{db.backend}_server_{code}"
    assert caught.value.details == {"server_message": message}


@pytest.mark.parametrize("kind", [ApacheHugeGraph, JanusGraph])
def test_successful_query_is_sent_unchanged_without_retry_rewrite_or_extra_index(kind):
    db, calls = adapter(kind, 200, "")
    result = db.execute(QUERY, {"_gsp_p0": 1}, 2.0)
    assert result["rows"] == [{"c0": 1}] and result["complete"] is True
    assert len(calls) == 1
    request = json.loads(calls[0][1]["body"])
    assert request["gremlin"] == f"({QUERY}).limit(11).toList()"
    assert request["bindings"] == {"_gsp_p0": 1}
