"""Protect leaf layers while the legacy runner is being migrated.

``app`` and ``experiment.runner`` are documented legacy exceptions in
``docs/architecture-baseline.md``.  New leaf layers may not recreate those
dependencies; the exceptions are removed in Tasks 6 and 11.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "src" / "graph_sym_perf"
FORBIDDEN = ("graph_sym_perf.cli", "graph_sym_perf.app", "graph_sym_perf.experiment", "graph_sym_perf.adapters")


def _imports_under(package: str) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / package).rglob("*.py"))


def test_current_leaf_layers_do_not_depend_on_orchestration_or_specific_adapters() -> None:
    for package in ("equivalence", "transforms"):
        imports = _imports_under(package)
        assert all(forbidden not in imports for forbidden in FORBIDDEN), package


def test_legacy_cross_layer_dependencies_are_explicitly_confined() -> None:
    runner = (ROOT / "experiment" / "runner.py").read_text(encoding="utf-8")
    assert "graph_sym_perf.app.gdsmith_mining" in runner
