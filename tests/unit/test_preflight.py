from __future__ import annotations

from graph_sym_perf.app.mining import _preflight
from graph_sym_perf.core import TargetSystem


class FakeAdapter:
    target = TargetSystem.NEO4J

    def __init__(
        self,
        *,
        dependency_ok: bool = True,
        environment_ok: bool = True,
        connectivity_ok: bool = True,
    ) -> None:
        self.dependency_ok = dependency_ok
        self.environment_ok = environment_ok
        self.connectivity_ok = connectivity_ok

    def check_dependency(self):
        return self.dependency_ok, "dependency"

    def check_environment(self):
        return self.environment_ok, ("MISSING_ENV",) if not self.environment_ok else ()

    def connectivity_check(self):
        return self.connectivity_ok, "connectivity"


def test_preflight_skips_missing_dependency() -> None:
    status = _preflight(FakeAdapter(dependency_ok=False))

    assert status.state == "skipped"
    assert not status.dependency_ok


def test_preflight_skips_missing_environment() -> None:
    status = _preflight(FakeAdapter(environment_ok=False))

    assert status.state == "skipped"
    assert status.missing_env == ("MISSING_ENV",)


def test_preflight_ready() -> None:
    status = _preflight(FakeAdapter())

    assert status.ready
