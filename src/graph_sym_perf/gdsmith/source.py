"""Convert accepted GDsmith records to GraphSymPerf query cases."""

from __future__ import annotations

from graph_sym_perf.core import QueryCase
from graph_sym_perf.gdsmith.parser import GdsmithStatementRecord


def query_cases_from_gdsmith(records: tuple[GdsmithStatementRecord, ...]) -> tuple[QueryCase, ...]:
    cases: list[QueryCase] = []
    for accepted_index, record in enumerate((item for item in records if item.accepted), start=1):
        case_id = f"gdsmith_case_{accepted_index:04d}"
        cases.append(
            QueryCase(
                case_id=case_id,
                template_name="gdsmith",
                query=record.statement,
                parameters={},
                base_predicate="",
                safe_expression="",
                source="gdsmith",
                generator_case_id=f"gdsmith_statement_{record.index}",
                raw_query=record.statement,
            )
        )
    return tuple(cases)
