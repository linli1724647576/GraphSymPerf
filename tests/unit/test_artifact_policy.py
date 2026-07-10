from graph_sym_perf.artifacts.policy import ArtifactLevel, ArtifactPolicy


def test_policy_promotes_only_candidates_and_issues() -> None:
    policy = ArtifactPolicy(shard_budget_mb=1)
    assert policy.level_for("accepted") is ArtifactLevel.MINIMAL
    assert policy.level_for("candidate") is ArtifactLevel.CANDIDATE
    assert policy.level_for("issue") is ArtifactLevel.ISSUE
