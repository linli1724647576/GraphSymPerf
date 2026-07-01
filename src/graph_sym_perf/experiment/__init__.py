"""Experiment orchestration boundary."""

from graph_sym_perf.experiment.aggregate import aggregate_run_root
from graph_sym_perf.experiment.doctor import run_doctor
from graph_sym_perf.experiment.manifest import ExecutionConfig, ExperimentManifest, load_experiment_manifest
from graph_sym_perf.experiment.runner import run_experiment
from graph_sym_perf.experiment.triage import run_triage, validate_triage

__all__ = [
    "ExecutionConfig",
    "ExperimentManifest",
    "aggregate_run_root",
    "load_experiment_manifest",
    "run_doctor",
    "run_experiment",
    "run_triage",
    "validate_triage",
]
