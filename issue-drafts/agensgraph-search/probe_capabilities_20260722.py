#!/usr/bin/env python3
"""Probe the Cypher surface accepted by AgensGraph 2.14.1."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Probe:
    name: str
    query: str


PROBES = (
    Probe("count", "MATCH (n:N) RETURN count(*)"),
    Probe("fixed_hop", "MATCH (:N)-[:R]->(:N) RETURN count(*)"),
    Probe("zero_hop", "MATCH (:N)-[:R*0..0]->(:N) RETURN count(*)"),
    Probe("one_hop_varlength", "MATCH (:N)-[:R*1..1]->(:N) RETURN count(*)"),
    Probe("relationship_type", "MATCH ()-[r]->() RETURN type(r), count(*)"),
    Probe("relationship_type_filter", "MATCH ()-[r]->() WHERE type(r) = 'R' RETURN count(*)"),
    Probe(
        "optional_non_null",
        "MATCH (n:N) OPTIONAL MATCH (n)-[:R]->(m:N) WHERE m IS NOT NULL RETURN count(*)",
    ),
    Probe("with_filter", "MATCH (n:N) WITH n WHERE n.id = 1 RETURN count(*)"),
    Probe("unwind_singleton", "MATCH (n:N) UNWIND [1] AS x RETURN count(*)"),
    Probe("inline_property", "MATCH (n:N {id: 1}) RETURN count(*)"),
    Probe("property_exists", "MATCH (n:N) WHERE exists(n.v) RETURN count(*)"),
    Probe("is_not_null", "MATCH (n:N) WHERE n.v IS NOT NULL RETURN count(*)"),
    Probe("singleton_in", "MATCH (n:N) WHERE n.id IN [1] RETURN count(*)"),
    Probe("pattern_predicate", "MATCH (n:N) WHERE (n)-[:R]->() RETURN count(*)"),
    Probe("case", "MATCH (n:N) RETURN sum(CASE WHEN n.v IS NULL THEN 0 ELSE 1 END)"),
    Probe("distinct", "MATCH (n:N) RETURN DISTINCT n.id ORDER BY n.id"),
    Probe("union_all", "MATCH (n:N) WHERE n.id = 1 RETURN n.id UNION ALL MATCH (n:N) WHERE n.id = 2 RETURN n.id"),
    Probe("path_length", "MATCH p=(:N)-[:R*0..1]->(:N) RETURN length(p), count(*) ORDER BY length(p)"),
    Probe("id_function", "MATCH (n:N) RETURN id(n), count(*)"),
    Probe("coalesce", "MATCH (n:N) RETURN count(coalesce(n.v, 0))"),
)


SETUP = """
DROP GRAPH IF EXISTS agens_probe CASCADE;
CREATE GRAPH agens_probe;
SET graph_path = agens_probe;
CREATE (:N {id: 1, v: 10}), (:N {id: 2, v: null}), (:N {id: 3, v: 30});
MATCH (a:N {id: 1}), (b:N {id: 2}) CREATE (a)-[:R {id: 11}]->(b);
"""


def run_psql(container: str, sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-U",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-X",
            "-q",
            "-A",
            "-t",
            "-F",
            "\t",
        ],
        input=sql,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    args = parser.parse_args()

    setup = run_psql(args.container, SETUP)
    if setup.returncode != 0:
        raise SystemExit(setup.stderr)

    supported = 0
    for probe in PROBES:
        result = run_psql(
            args.container,
            f"SET graph_path = agens_probe;\n{probe.query};\n",
        )
        record: dict[str, Any] = {
            "name": probe.name,
            "query": probe.query,
            "supported": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
        supported += int(record["supported"])
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    print(json.dumps({"supported": supported, "total": len(PROBES)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
