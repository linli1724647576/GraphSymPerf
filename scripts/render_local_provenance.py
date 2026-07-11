#!/usr/bin/env python3
"""Print non-secret local execution artifact pins for the formal .env."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graph_sym_perf.experiment.provenance import installed_distribution_digest


_CONTAINERS = {
    "NEO4J": "graphsymperf-neo4j",
    "MEMGRAPH": "graphsymperf-memgraph",
    "FALKORDB": "graphsymperf-falkordb",
    "AGENSGRAPH": "graphsymperf-agensgraph",
    "ARCADEDB": "graphsymperf-arcadedb",
    "APACHE_AGE": "graphsymperf-apache-age",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", default=os.environ.get("GRAPHSYMPERF_GDSMITH_JAR"))
    args = parser.parse_args(argv)
    if not args.jar:
        raise SystemExit("--jar or GRAPHSYMPERF_GDSMITH_JAR is required")
    jar = Path(args.jar).expanduser().resolve(strict=True)

    for target, container in _CONTAINERS.items():
        completed = subprocess.run(
            ("docker", "inspect", "--format={{.Image}}", container),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        digest = completed.stdout.strip() if completed.returncode == 0 else ""
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise RuntimeError(f"cannot resolve Docker image ID for {container}")
        print(f"GRAPHSYMPERF_{target}_CONTAINER={container}")
        print(f"GRAPHSYMPERF_{target}_IMAGE_DIGEST={digest}")

    ladybug = installed_distribution_digest("ladybug")
    if ladybug is None:
        raise RuntimeError("cannot hash the installed ladybug distribution")
    print(f"GRAPHSYMPERF_LADYBUGDB_PACKAGE_DIGEST={ladybug}")
    print(f"GRAPHSYMPERF_GDSMITH_JAR={jar}")
    print(f"GRAPHSYMPERF_GDSMITH_JAR_SHA256=sha256:{_sha256(jar)}")
    return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
