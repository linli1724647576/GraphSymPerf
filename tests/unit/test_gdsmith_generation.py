import pytest

from graph_sym_perf.generation.gdsmith import GdsmithGenerator, GenerationRequest, derive_generation_seed


def test_gdsmith_generator_is_the_only_generation_seam() -> None:
    generator = GdsmithGenerator(lambda request: ("MATCH (n) RETURN n",))
    corpus = generator.generate(GenerationRequest(seed=1, limit=1))
    assert corpus.queries[0].source == "gdsmith"
    with pytest.raises(ValueError):
        GdsmithGenerator(lambda request: (), source="alternate")


def test_generation_seed_is_bound_to_experiment_dbms_shard_and_batch() -> None:
    seed = derive_generation_seed(7, "neo4j", 3, 2)
    assert seed == derive_generation_seed(7, "neo4j", 3, 2)
    assert len(
        {
            seed,
            derive_generation_seed(7, "memgraph", 3, 2),
            derive_generation_seed(7, "neo4j", 4, 2),
            derive_generation_seed(7, "neo4j", 3, 3),
        }
    ) == 4


def test_gdsmith_corpus_deduplicates_alpha_equivalent_ast_hashes() -> None:
    generator = GdsmithGenerator(
        lambda request: (
            "MATCH (n:A) RETURN n",
            "MATCH (m:A) RETURN m",
            "MATCH (n:B) RETURN n",
        )
    )

    corpus = generator.generate(GenerationRequest(seed=1, limit=3))

    assert len(corpus.queries) == 2
    assert len(corpus.duplicate_ast_hashes) == 1
    assert len({query.ast_hash for query in corpus.queries}) == 2


@pytest.mark.parametrize("seed,limit", [(-1, 1), (1, 0), (True, 1)])
def test_generation_request_rejects_invalid_bounds(seed, limit) -> None:
    with pytest.raises((TypeError, ValueError)):
        GenerationRequest(seed=seed, limit=limit)
