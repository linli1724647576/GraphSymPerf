import argparse
import json
from pathlib import Path
import yaml
from databases import select_database
from data_structures.graph import Graph
from data_structures.query import Query
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
    result = replay_case(config["replay"]["case"], config)
    print(json.dumps({key: value for key, value in result.items() if key != "plans"}, ensure_ascii=False, indent=2))
    if result.get("stop_run"):
        raise RuntimeError("Replay stopped: query termination is unconfirmed")


def replay_case(directory, config):
    directory = Path(directory)
    record = json.loads((directory / "result.json").read_text())
    # Earlier archives contain Neo4j Cypher with inline values; decode that format here once.
    # 迁移前归档固定为 Neo4j Cypher 且常量内联，仅在读取入口补齐这些已知信息。
    archived = record["config"]
    language = archived.get("language", "cypher")
    system = archived["database"].get("system", "neo4j")
    if language != config["language"] or system != config["database"]["system"]:
        raise ValueError("Replay requires the archived language and database system")
    if (language not in {"cypher", "gremlin"}
            or (language == "gremlin" and system not in {"arcadedb", "janusgraph", "hugegraph"})
            or (language == "cypher" and system in {"janusgraph", "hugegraph"})):
        raise ValueError("Unsupported replay language and database combination")
    graph = Graph(**json.loads((directory / "graph.json").read_text()))
    parameters = record.get("parameters", {})
    columns = record.get("columns", {})
    queries = [Query((directory / f"query_{name}.{language}").read_text(), parameters.get(name, {}),
                     tuple(columns[name]) if columns.get(name) is not None else None) for name in ("a", "b")]
    backend = select_database(config["database"]["system"])
    db = backend.Database(config["database"], language=language) if system == "arcadedb" else backend.Database(config["database"])
    try:
        db.load(graph)
        # Use the current connection but the archived measurement settings and query text.
        # 使用当前数据库连接，以及归档中的测量参数和查询文本。
        result = run_pair(db, *queries, record["config"]["measurement"], check_only=config["replay"]["check_only"])
        if "workload" in record:
            # Reuse construction metadata, never an earlier run's execution assessment.
            # 复用构图信息，不继承先前执行的工作量判定。
            result["workload"] = {**record["workload"], "opportunity": "unknown",
                                  "execution": {"a": "unknown", "b": "unknown"}}
        result["database_version"] = db.version
        (directory / "replay-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        db.close()


if __name__ == "__main__":
    main()
