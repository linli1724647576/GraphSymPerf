from graph_sym_perf.reducer.ddmin import ddmin


def test_ddmin_finds_one_minimal_subset_under_symptom_predicate() -> None:
    result = ddmin(("a", "keep", "b", "c"), lambda candidate: "keep" in candidate)
    assert result == ("keep",)
