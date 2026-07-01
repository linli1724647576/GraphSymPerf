from __future__ import annotations


def test_package_import() -> None:
    import graph_sym_perf

    assert graph_sym_perf.__version__ == "0.0.0"


def test_cli_main_import() -> None:
    from graph_sym_perf.cli.__main__ import main

    assert callable(main)
