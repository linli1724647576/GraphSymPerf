from __future__ import annotations

from graph_sym_perf.config.env_file import environment_with_dotenv


def test_environment_with_dotenv_loads_file_and_preserves_explicit_env(tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        """
# comment
export GRAPHSYMPERF_A=from_file
GRAPHSYMPERF_B="quoted"
""",
        encoding="utf-8",
    )

    loaded = environment_with_dotenv(None, dotenv_path=dotenv)
    explicit = environment_with_dotenv({}, dotenv_path=dotenv)

    assert loaded["GRAPHSYMPERF_A"] == "from_file"
    assert loaded["GRAPHSYMPERF_B"] == "quoted"
    assert explicit == {}
