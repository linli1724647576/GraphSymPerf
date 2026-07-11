from __future__ import annotations

from graph_sym_perf.workload.registry import AcceptedCorpusRegistry


def test_cross_shard_registry_is_atomic_and_idempotent_for_owner(tmp_path) -> None:
    registry = AcceptedCorpusRegistry(tmp_path / "accepted_corpus_registry.jsonl")

    assert registry.reserve(
        source_ast_hash="a" * 64,
        pair_hash="b" * 64,
        target="neo4j",
        shard_index=0,
    ) is None
    assert registry.reserve(
        source_ast_hash="a" * 64,
        pair_hash="b" * 64,
        target="neo4j",
        shard_index=0,
    ) is None
    assert registry.reserve(
        source_ast_hash="a" * 64,
        pair_hash="c" * 64,
        target="neo4j",
        shard_index=1,
    ) == "CROSS_SHARD_DUPLICATE_AST"
    assert registry.reserve(
        source_ast_hash="d" * 64,
        pair_hash="b" * 64,
        target="memgraph",
        shard_index=0,
    ) == "CROSS_SHARD_DUPLICATE_PAIR"


def test_registry_rejects_corrupt_or_noncanonical_records(tmp_path) -> None:
    path = tmp_path / "registry.jsonl"
    path.write_text('{"source_ast_hash":"short"}\n', encoding="utf-8")

    registry = AcceptedCorpusRegistry(path)
    try:
        registry.reserve(
            source_ast_hash="a" * 64,
            pair_hash="b" * 64,
            target="neo4j",
            shard_index=0,
        )
    except OSError as exc:
        assert "corrupt" in str(exc)
    else:
        raise AssertionError("corrupt registry must fail closed")
