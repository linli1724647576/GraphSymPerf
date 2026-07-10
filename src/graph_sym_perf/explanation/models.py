from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from graph_sym_perf.validation.models import PipelineStage


@dataclass(frozen=True)
class ExplanationEvidence:
    issue_stage: PipelineStage
    normalized_plan: Mapping[str, object] | None
    profile_metrics: Mapping[str, object] | None
