from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "src" / "graph_sym_perf"


def test_runtime_first_tree_has_no_legacy_orchestration_packages() -> None:
    for relative in ("app/mining.py", "coverage/pmc.py", "feedback/scheduler.py", "oracle/plan_oracle.py", "experiment/runner.py"):
        assert not (ROOT / relative).exists(), relative


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return tuple(names)


def test_leaf_layers_do_not_depend_on_orchestration_or_concrete_adapters() -> None:
    forbidden = (
        "graph_sym_perf.cli",
        "graph_sym_perf.app",
        "graph_sym_perf.experiment",
        "graph_sym_perf.adapters",
    )
    for package in ("runtime", "equivalence", "transforms"):
        for path in (ROOT / package).rglob("*.py"):
            assert all(not name.startswith(forbidden) for name in _imports(path)), path


def test_cli_depends_on_experiment_public_surface_not_leaf_implementations() -> None:
    for path in (ROOT / "cli").rglob("*.py"):
        application_imports = {
            name
            for name in _imports(path)
            if name.startswith("graph_sym_perf.") and not name.startswith("graph_sym_perf.cli")
        }
        assert application_imports <= {"graph_sym_perf.experiment", "graph_sym_perf.issues"}, (
            path,
            sorted(application_imports),
        )


def test_experiment_has_no_reverse_dependency_on_removed_app_package() -> None:
    for path in (ROOT / "experiment").rglob("*.py"):
        assert all(not name.startswith("graph_sym_perf.app") for name in _imports(path)), path
