from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "src" / "graph_sym_perf"


def test_runtime_first_tree_has_no_legacy_orchestration_packages() -> None:
    for relative in ("app/mining.py", "coverage/pmc.py", "feedback/scheduler.py", "oracle/plan_oracle.py", "experiment/runner.py"):
        assert not (ROOT / relative).exists(), relative


def test_leaf_layers_do_not_depend_on_cli_or_adapters() -> None:
    forbidden = ("graph_sym_perf.cli", "graph_sym_perf.adapters")
    for package in ("runtime", "equivalence", "validation", "transforms"):
        imports = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / package).rglob("*.py"))
        assert all(token not in imports for token in forbidden), package
