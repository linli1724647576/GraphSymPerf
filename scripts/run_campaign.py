"""Run a fixed seed/scale campaign from config.yaml on one selected database."""
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import random
import resource
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import load_config, language_for, prepare_case
from databases import select_database
from data_structures.case import Inapplicable
from executor import run_pair, save_case


def main():
    config = load_config()
    settings = config["campaign"]
    for name in ("seeds", "high_units"):
        values = settings[name]
        minimum = 1 if name == "high_units" else 0
        if not values or len(values) != len(set(values)) or any(type(v) is not int or v < minimum for v in values):
            raise ValueError(f"campaign.{name} must contain distinct valid integers")
    for name in ("minutes_per_target", "max_nodes", "max_relationships"):
        if type(settings[name]) is not int or settings[name] <= 0:
            raise ValueError(f"campaign.{name} must be a positive integer")
    if config["run"]["mode"] != "experiment" or not config["run"]["save_all"]:
        raise ValueError("Campaigns require experiment mode and save_all=true")
    output = Path(config["run"]["output"])
    output.mkdir(parents=True, exist_ok=False)
    module, support = language_for(config)
    patterns = module.patterns_for(support)
    rng = random.Random(config["run"]["seed"])
    records = []
    for seed in settings["seeds"]:
        names = list(config["run"]["patterns"])
        rng.shuffle(names)
        for name in names:
            for units in settings["high_units"]:
                records.append({"pattern": name, "seed": seed, "high_units": units, "status": "not_attempted"})
    (output / "attempts.json").write_text(json.dumps(records, indent=2))
    started, cpu_started = time.monotonic(), time.process_time()
    deadline = started + settings["minutes_per_target"] * 60
    backend = select_database(config["database"]["system"])
    extra = {"language": config["language"]} if config["database"]["system"] == "arcadedb" else {}
    db = backend.Database(config["database"], **extra)
    stop_reason = "completed"
    try:
        for number, row in enumerate(records, 1):
            if time.monotonic() >= deadline:
                stop_reason = "budget_exhausted"
                break
            config["run"]["case_seed"] = row["seed"]
            config["graph"]["high_units"] = row["high_units"]
            start = time.monotonic()
            try:
                case, graph, a, b = prepare_case(patterns[row["pattern"]], row["seed"], config)
            except Inapplicable as error:
                row.update(status="skipped", reason=str(error))
            except (Exception, KeyboardInterrupt) as error:
                status = "interrupted" if isinstance(error, (KeyboardInterrupt, InterruptedError)) else "error"
                row.update(status=status, phase="generation", error={"type": type(error).__name__, "message": str(error)}, traceback=traceback.format_exc())
            else:
                row.update(nodes=len(graph.nodes), relationships=len(graph.relationships), generation_seconds=time.monotonic() - start)
                if row["nodes"] > settings["max_nodes"] or row["relationships"] > settings["max_relationships"]:
                    row.update(status="scale_limit")
                else:
                    directory = output / f"{number:05d}-{row['pattern']}-{row['seed']}-{row['high_units']}"
                    row.update(status="running", archive=str(directory))
                    # Fingerprints exclude seeds and execution metadata; equal inputs remain equal.
                    # 指纹不包含种子和执行信息，使相同输入具有相同指纹。
                    queries = {"a": asdict(a), "b": asdict(b)}
                    row["query_fingerprint"] = sha256(json.dumps(queries, sort_keys=True).encode()).hexdigest()
                    row["input_fingerprint"] = sha256(json.dumps({"queries": queries, "graph": asdict(graph)}, sort_keys=True).encode()).hexdigest()
                    save_case(directory, case, graph, a, b, {"status": "running"}, config, db.version)
                    (output / "attempts.json").write_text(json.dumps(records, indent=2))
                    phase, phase_started = "loading", time.monotonic()
                    try:
                        db.load(graph)
                        row["load_seconds"] = time.monotonic() - phase_started
                        phase, phase_started = "measurement_and_profile", time.monotonic()
                        result = run_pair(db, a, b, config["measurement"])
                    except (Exception, KeyboardInterrupt) as error:
                        # Preserve native cancellation details when the user interrupts execution.
                        # 用户中断执行时保留原生取消响应，不把中断报告为正常完成。
                        status = "interrupted" if isinstance(error, (KeyboardInterrupt, InterruptedError)) else "error"
                        result = {"status": status, "error": {"phase": phase, "type": type(error).__name__, "message": str(error)}, "traceback": traceback.format_exc()}
                        if getattr(db, "cancellation", None):
                            result["cancellation"] = db.cancellation
                    row[phase + "_seconds"] = time.monotonic() - phase_started
                    row.update(status=result["status"], rows=result.get("rows"), profile_status=result.get("profile_status"), stop_run=result.get("stop_run", False))
                    if "error" in result:
                        row["error"] = result["error"]
                    save_case(directory, case, graph, a, b, result, config, db.version)
            row["wall_seconds"] = time.monotonic() - start
            (output / "attempts.json").write_text(json.dumps(records, indent=2))
            print(f"{number:03d} {row['pattern']} seed={row['seed']} units={row['high_units']} {row['status']}", flush=True)
            # Stop this target without retries; the unsubmitted manifest entries remain visible.
            # 本组停止后不重试，清单中保留尚未提交的用例。
            if row["status"] in {"error", "interrupted", "result_mismatch"} or row.get("stop_run"):
                stop_reason = "termination_unconfirmed" if row.get("stop_run") else row["status"]
                break
    finally:
        db.close()
        for row in records:
            if row["status"] == "not_attempted":
                row["reason"] = stop_reason
        (output / "attempts.json").write_text(json.dumps(records, indent=2))
        usage = resource.getrusage(resource.RUSAGE_SELF)
        report = {"finished_at": datetime.now().astimezone().isoformat(), "stop_reason": stop_reason,
                  "wall_seconds": time.monotonic() - started, "client_cpu_seconds": time.process_time() - cpu_started,
                  "client_peak_rss_bytes": usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                  "database_version": db.version, "counts": dict(Counter(row["status"] for row in records))}
        (output / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    if stop_reason == "interrupted":
        raise SystemExit(130)


if __name__ == "__main__":
    main()
