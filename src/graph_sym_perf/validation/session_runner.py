"""Fresh-process validation session lifecycle and measurement."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import re
import uuid
from dataclasses import replace
from typing import Any, Callable

from graph_sym_perf.adapters.lifecycle import reset_graph_state
from graph_sym_perf.equivalence.checker import compare_execution_results
from graph_sym_perf.runtime.measurement import RuntimeHarness
from graph_sym_perf.runtime.models import RuntimeEvidence, RuntimePolicy, RuntimeVerdict
from graph_sym_perf.validation.models import (
    CandidateBundle,
    EquivalenceEvidence,
    SessionLifecycle,
    ValidationSession,
)
from graph_sym_perf.validation.graph_snapshot import verify_graph_snapshot


class SubprocessSessionRunner:
    """Run one validation session in a fresh OS process and adapter instance."""

    def __init__(
        self,
        adapter_factory: Callable[[CandidateBundle], Any],
    ) -> None:
        self._adapter_factory = adapter_factory

    def run_session(
        self,
        candidate: CandidateBundle,
        session_number: int,
        *,
        expected_slow_query: str,
        noise_floor_ms: float,
        timer_resolution_ms: float,
    ) -> tuple[EquivalenceEvidence, ValidationSession]:
        policy = candidate.validation_policy
        if (
            expected_slow_query != candidate.discovery_slow_query
            or noise_floor_ms != policy.noise_floor_ms
            or timer_resolution_ms != policy.timer_resolution_ms
        ):
            return _failed_process_result(candidate, session_number, -1, "validation_policy_mismatch")
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(
            target=_run_validation_session,
            args=(
                send,
                self._adapter_factory,
                candidate,
                session_number,
                expected_slow_query,
                noise_floor_ms,
                timer_resolution_ms,
                candidate.validation_policy.query_timeout_seconds,
                candidate.validation_policy.max_result_rows,
            ),
        )
        process.start()
        process_id = process.pid or -1
        send.close()
        if not receive.poll(candidate.validation_policy.session_timeout_seconds):
            receive.close()
            _terminate_process(process)
            return _failed_process_result(candidate, session_number, process_id, "session_process_timeout")
        try:
            equivalence, session = receive.recv()
        except EOFError:
            _terminate_process(process)
            return _failed_process_result(candidate, session_number, process_id, "session_process_crashed")
        finally:
            receive.close()
        process.join(timeout=5.0)
        exited = not process.is_alive() and process.exitcode == 0
        if process.is_alive():
            _terminate_process(process)
        else:
            process.close()
        if session.lifecycle is not None:
            session = replace(session, lifecycle=replace(session.lifecycle, process_exited=exited))
        return equivalence, session


def _run_validation_session(
    send: Any,
    adapter_factory: Callable[[CandidateBundle], Any],
    candidate: CandidateBundle,
    session_number: int,
    expected_slow_query: str,
    noise_floor_ms: float,
    timer_resolution_ms: float,
    query_timeout_seconds: float,
    max_result_rows: int,
) -> None:
    adapter = None
    lifecycle = {
        "process_started": True,
        "adapter_created": False,
        "connection_opened": False,
        "graph_reset": False,
        "graph_loaded": False,
        "graph_verified": False,
        "connection_closed": False,
        "process_exited": False,
        "connection_id": "",
    }
    graph_fingerprint = ""
    dbms_fingerprint = ""
    equivalence = EquivalenceEvidence(False, False, ("session_not_completed",))
    evidence = _invalid_runtime_evidence(candidate, "session_not_completed")
    controls: tuple[RuntimeEvidence, ...] = ()
    health_event: str | None = None
    try:
        adapter = adapter_factory(candidate)
        lifecycle["adapter_created"] = True
        connected = adapter.connectivity_check()
        if not isinstance(connected, tuple) or not connected or connected[0] is not True:
            raise RuntimeError("connection_check_failed")
        lifecycle["connection_opened"] = True
        lifecycle["connection_id"] = _connection_identity(adapter)
        reset_graph_state(
            adapter,
            query_kind="validation_reset",
            timeout_seconds=query_timeout_seconds,
        )
        lifecycle["graph_reset"] = True
        adapter.load_gdsmith_graph(candidate.graph_snapshot.statements)
        lifecycle["graph_loaded"] = True
        graph_verification = verify_graph_snapshot(
            adapter,
            candidate.graph_snapshot,
            max_rows=max_result_rows,
            timeout_seconds=query_timeout_seconds,
        )
        if not graph_verification.verified:
            raise RuntimeError(graph_verification.reason_codes[0])
        graph_fingerprint = str(graph_verification.fingerprint or "")
        lifecycle["graph_verified"] = True
        fingerprint = adapter.dbms_fingerprint()
        if not getattr(fingerprint, "available", False):
            raise RuntimeError("dbms_fingerprint_unavailable")
        dbms_fingerprint = str(fingerprint.value)
        if dbms_fingerprint != candidate.pair.dbms_fingerprint:
            raise RuntimeError("dbms_fingerprint_mismatch")
        left = adapter.execute(
            candidate.pair.query_a,
            candidate.pair.parameters,
            "validation_equivalence_a",
            max_rows=max_result_rows,
            timeout_seconds=query_timeout_seconds,
        )
        right = adapter.execute(
            candidate.pair.query_b,
            candidate.pair.parameters,
            "validation_equivalence_b",
            max_rows=max_result_rows,
            timeout_seconds=query_timeout_seconds,
        )
        typed = compare_execution_results(
            left,
            right,
            ordered=_has_order_by(candidate.pair.query_a) or _has_order_by(candidate.pair.query_b),
            max_result_rows=max_result_rows,
        )
        equivalence = EquivalenceEvidence(typed.equivalent, typed.complete_result, typed.reason_codes)
        if not typed.equivalent or not typed.complete_result:
            raise RuntimeError(typed.reason_codes[0] if typed.reason_codes else "equivalence_failed")
        for side, query in (("a", candidate.pair.query_a), ("b", candidate.pair.query_b)):
            first_control_result = adapter.execute(
                query,
                candidate.pair.parameters,
                f"validation_control_equivalence_{side}_1",
                max_rows=max_result_rows,
                timeout_seconds=query_timeout_seconds,
            )
            second_control_result = adapter.execute(
                query,
                candidate.pair.parameters,
                f"validation_control_equivalence_{side}_2",
                max_rows=max_result_rows,
                timeout_seconds=query_timeout_seconds,
            )
            control_equivalence = compare_execution_results(
                first_control_result,
                second_control_result,
                ordered=_has_order_by(query),
                max_result_rows=max_result_rows,
            )
            if not control_equivalence.equivalent or not control_equivalence.complete_result:
                equivalence = EquivalenceEvidence(False, False, ("negative_control_result_unstable",))
                raise RuntimeError("negative_control_result_unstable")
        harness = RuntimeHarness()
        control_policy = RuntimePolicy(
            warmups=0,
            pairs=20,
            seed=session_number,
            require_exact_balance=True,
            timeout_ms=query_timeout_seconds * 1000,
        )
        controls = (
            harness.measure_pair(
                adapter,
                candidate.pair.query_a,
                candidate.pair.query_a,
                parameters=candidate.pair.parameters,
                policy=control_policy,
            ),
            harness.measure_pair(
                adapter,
                candidate.pair.query_b,
                candidate.pair.query_b,
                parameters=candidate.pair.parameters,
                policy=replace(control_policy, seed=session_number + 10_000),
            ),
        )
        evidence = harness.measure_validation(
            adapter,
            candidate.pair,
            policy=RuntimePolicy(
                warmups=5,
                pairs=20,
                seed=session_number,
                require_exact_balance=True,
                timeout_ms=query_timeout_seconds * 1000,
            ),
            expected_slow_query=expected_slow_query,
            noise_floor_ms=noise_floor_ms,
            timer_resolution_ms=timer_resolution_ms,
        )
        health_reader = getattr(adapter, "health_event", None)
        health_event = health_reader() if callable(health_reader) else None
    except Exception as exc:
        reason = str(exc) if str(exc) else type(exc).__name__
        if equivalence.reason_codes == ("session_not_completed",):
            equivalence = EquivalenceEvidence(False, False, (reason,))
        evidence = _invalid_runtime_evidence(candidate, reason)
    finally:
        if adapter is not None:
            try:
                adapter.close()
                lifecycle["connection_closed"] = True
            except Exception as exc:
                health_event = f"close_failed:{type(exc).__name__}"
        session = ValidationSession(
            session_id=f"validation-{session_number}-{uuid.uuid4().hex}",
            process_id=os.getpid(),
            graph_fingerprint=graph_fingerprint,
            dbms_fingerprint=dbms_fingerprint,
            evidence=evidence,
            negative_controls=controls,
            health_event=health_event,
            lifecycle=SessionLifecycle(**lifecycle),
        )
        try:
            send.send((equivalence, session))
        finally:
            send.close()


def _connection_identity(adapter: Any) -> str:
    explicit = getattr(adapter, "connection_identity", None)
    if callable(explicit):
        explicit = explicit()
    if explicit:
        return f"{os.getpid()}:{explicit}"
    for name in ("_driver", "_conn", "_database", "_client"):
        connection = getattr(adapter, name, None)
        if connection is not None:
            return f"{os.getpid()}:{type(connection).__qualname__}:{id(connection)}"
    return f"{os.getpid()}:{type(adapter).__qualname__}:{id(adapter)}"


def _invalid_runtime_evidence(candidate: CandidateBundle, reason: str) -> RuntimeEvidence:
    return RuntimeEvidence(
        verdict=RuntimeVerdict.INVALID,
        query_a_hash=hashlib.sha256(candidate.pair.query_a.encode("utf-8")).hexdigest(),
        query_b_hash=hashlib.sha256(candidate.pair.query_b.encode("utf-8")).hexdigest(),
        paired_samples=(),
        median_ratio=None,
        median_delta_ms=None,
        ci_ratio_low=None,
        ci_ratio_high=None,
        direction_consistency=0.0,
        relative_mad=None,
        reason_codes=(reason,),
        slow_query=candidate.discovery_slow_query,
        graph_snapshot_hash=candidate.pair.graph_snapshot_hash,
        dbms_fingerprint=candidate.pair.dbms_fingerprint,
    )


def _failed_process_result(
    candidate: CandidateBundle,
    session_number: int,
    process_id: int,
    reason: str,
) -> tuple[EquivalenceEvidence, ValidationSession]:
    lifecycle = SessionLifecycle(True, False, False, False, False, False, False, True, "")
    return (
        EquivalenceEvidence(False, False, (reason,)),
        ValidationSession(
            f"validation-{session_number}-failed",
            process_id,
            "",
            "",
            _invalid_runtime_evidence(candidate, reason),
            (),
            health_event=reason,
            lifecycle=lifecycle,
        ),
    )


def _has_order_by(query: str) -> bool:
    return re.search(r"\bORDER\s+BY\b", query, re.IGNORECASE) is not None


def _terminate_process(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
        process.join(timeout=2.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=2.0)
    if not process.is_alive():
        process.close()
