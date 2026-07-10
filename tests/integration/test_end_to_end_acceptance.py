import hashlib
import json

from graph_sym_perf.experiment.plan_models import RunPlan, SourceRevision
from graph_sym_perf.experiment.supervisor import ShardSupervisor


class _AcceptanceWorker:
    def __init__(self) -> None:
        self.context = None

    def bind_attempt_context(self, context) -> None:
        self.context = context

    def __call__(self, task):
        assert self.context is not None
        count = 1000 if task == "complete" else 999
        hashes = [hashlib.sha256(f"{task}:{index}".encode()).hexdigest() for index in range(count)]
        (self.context.shard_dir / "accepted_pairs.jsonl").write_text(
            "".join(json.dumps({"pair_hash": value}) + "\n" for value in hashes), encoding="utf-8"
        )
        stages = {}
        for stage in ("screening", "validation", "reduction"):
            path = self.context.attempt_dir / f"{stage}.json"
            path.write_text(stage, encoding="utf-8")
            stages[stage] = {
                "path": str(path.relative_to(self.context.shard_dir)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        return {"accepted_pairs": count, "accepted_pair_hashes": hashes, "stage_evidence": stages}


def test_supervisor_counts_only_exactly_complete_shards(tmp_path) -> None:
    plan = RunPlan("acceptance", 2, ("complete", "short"), 2_000, SourceRevision(None, None, None))  # type: ignore[arg-type]
    summary = ShardSupervisor(tmp_path).run(plan, _AcceptanceWorker())
    assert summary.completed == ("complete",)
    assert summary.incomplete == ("short",)
