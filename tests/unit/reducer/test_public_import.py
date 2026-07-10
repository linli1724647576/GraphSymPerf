from graph_sym_perf.reducer import (
    CandidateReducer,
    PartialManualReview,
    ReductionBundle,
    ReductionReport,
    ReductionStatus,
    RuleWitness,
)
from graph_sym_perf.reducer.reducer import CandidateReducer as ConcreteCandidateReducer
from graph_sym_perf.reducer.models import (
    PartialManualReview as ConcretePartialManualReview,
    ReductionBundle as ConcreteReductionBundle,
    ReductionReport as ConcreteReductionReport,
    ReductionStatus as ConcreteReductionStatus,
    RuleWitness as ConcreteRuleWitness,
)


def test_public_reducer_import_resolves_to_runtime_preserving_reducer() -> None:
    assert CandidateReducer is ConcreteCandidateReducer
    assert ReductionBundle is ConcreteReductionBundle
    assert ReductionReport is ConcreteReductionReport
    assert ReductionStatus is ConcreteReductionStatus
    assert RuleWitness is ConcreteRuleWitness
    assert PartialManualReview is ConcretePartialManualReview
