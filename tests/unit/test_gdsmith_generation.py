import pytest

from graph_sym_perf.generation.gdsmith import GdsmithGenerator, GenerationRequest


def test_gdsmith_generator_is_the_only_generation_seam() -> None:
    generator = GdsmithGenerator(lambda request: ("MATCH (n) RETURN n",))
    corpus = generator.generate(GenerationRequest(seed=1, limit=1))
    assert corpus.queries[0].source == "gdsmith"
    with pytest.raises(ValueError):
        GdsmithGenerator(lambda request: (), source="ldbc")
