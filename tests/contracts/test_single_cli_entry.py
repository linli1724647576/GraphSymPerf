from pathlib import Path


def test_package_exposes_exactly_one_console_entry() -> None:
    project = Path("pyproject.toml").read_text(encoding="utf-8")
    scripts = project.split("[project.scripts]", 1)[1].split("[", 1)[0]

    entries = tuple(line for line in scripts.splitlines() if "=" in line)

    assert entries == ('graphsymperf = "graph_sym_perf.cli.__main__:main"',)


def test_cli_does_not_construct_leaf_pipeline_components() -> None:
    source = Path("src/graph_sym_perf/cli/__main__.py").read_text(encoding="utf-8")

    forbidden = (
        "RuntimeFirstShardWorker",
        "ExperimentLifecycle",
        "IssueValidator",
        "CandidateReducer",
        "export_issue",
    )

    assert not any(name in source for name in forbidden)
