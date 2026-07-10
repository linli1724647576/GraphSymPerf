from graph_sym_perf.cli.runtime_first import main


def test_runtime_first_plan_prints_exact_formal_budget(capsys) -> None:
    assert main(["experiment", "plan", "--config", "config/experiment.yaml"]) == 0
    assert "shards=112" in capsys.readouterr().out
