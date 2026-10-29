import argparse
import json
from pathlib import Path
import yaml
from databases.neo4j import Neo4j
from data_structures.graph import Graph
from executor import run_pair


def main():
    parser = argparse.ArgumentParser(description="Replay the case selected in config.yaml. Replaces all graph data.")
    parser.parse_args()
    with open("config.yaml") as file:
        config = yaml.safe_load(file)
    if not config["replay"]["case"]:
        raise ValueError("Set replay.case in config.yaml before replaying")
    if type(config["replay"]["check_only"]) is not bool:
        raise ValueError("replay.check_only must be a boolean")
    directory = Path(config["replay"]["case"])
    record = json.loads((directory / "result.json").read_text())
    graph = Graph(**json.loads((directory / "graph.json").read_text()))
    queries = [(directory / name).read_text() for name in ("query_a.cypher", "query_b.cypher")]
    db = Neo4j(config["database"])
    try:
        db.load(graph)
        # Use the current connection but the archived measurement settings and query text.
        # 使用当前数据库连接，以及归档中的测量参数和查询文本。
        result = run_pair(db, *queries, record["config"]["measurement"], check_only=config["replay"]["check_only"])
        result["database_version"] = db.version
        (directory / "replay-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({key: value for key, value in result.items() if key != "plans"}, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
