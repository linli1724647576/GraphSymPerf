from __future__ import annotations

from graph_sym_perf.adapters import create_adapter
from graph_sym_perf.core import (
    ConfirmationConfig,
    DatasetConfig,
    EquivalenceConfig,
    GdsmithConfig,
    OracleConfig,
    QueryGenerationConfig,
    RunConfig,
    RunPaths,
    SystemConfig,
    TargetSystem,
    TransformConfig,
)


def _run_config() -> RunConfig:
    return RunConfig(
        source_path="config/test.yaml",
        paths=RunPaths(),
        systems=(),
        dataset=DatasetConfig(),
        query_generation=QueryGenerationConfig(gdsmith=GdsmithConfig()),
        equivalence=EquivalenceConfig(),
        confirmation=ConfirmationConfig(),
        oracle=OracleConfig(),
        transforms=TransformConfig(),
    )


def test_factory_supports_all_registered_targets() -> None:
    config = _run_config()
    for target in TargetSystem:
        adapter = create_adapter(
            SystemConfig(target=target, required_env=(), optional_env={}, settings={}),
            config,
            {},
        )
        assert adapter.target is target
