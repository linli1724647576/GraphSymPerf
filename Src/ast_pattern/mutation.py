"""M5 applies one declared local rule to the immutable M4 checkpoint."""

from dataclasses import replace

from symbolic import finish_mutation

from .contracts import StageFailure, digest
from .state_builder import validate_state_and_plan


def mutate_target(instantiated, graph_state, target_id):
    embedded, plan, record = instantiated.embedded, instantiated.plan, instantiated.record
    validate_state_and_plan(embedded, graph_state, plan)
    if target_id != embedded.target_id or record.get("target_id") != target_id:
        raise StageFailure("M5", "premise_failed", "mutation target does not match the registered instance")
    if graph_state.graph_digest != instantiated.state.graph_digest:
        raise StageFailure("M5", "contract_drift", "mutation was given a different graph")
    checks = (("source_digest", instantiated.source), ("replacement_digest", instantiated.replacement),
              ("bindings_digest", instantiated.bindings), ("checks_digest", instantiated.checks))
    if any(record.get(key) != digest(value) for key, value in checks):
        raise StageFailure("M5", "contract_drift", "the M4 source, alternative, bindings or checks changed")
    if record.get("embedded_digest") != embedded.embedded_digest or record.get("binding_plan_digest") != plan.plan_digest:
        raise StageFailure("M5", "contract_drift", "the M4 input contract changed")
    try:
        pair = finish_mutation(instantiated.prepared)
    except ValueError as error:
        raise StageFailure("M5", "premise_failed", str(error)) from error
    if pair.query_a != instantiated.source:
        raise StageFailure("M5", "out_of_region_mutation", "the mutation helper changed its source")
    mutation = {**pair.mutation_record, "method_stage": "M5", "target_id": target_id,
                "graph_digest": graph_state.graph_digest, "binding_plan_digest": plan.plan_digest,
                "embedded_digest": embedded.embedded_digest,
                "source_digest": digest(pair.query_a), "target_digest": digest(pair.query_b)}
    bindings = {**pair.bindings, "target_id": target_id, "mutation_direction": mutation["direction"],
                "graph_digest": graph_state.graph_digest, "binding_plan_digest": plan.plan_digest}
    return replace(pair, bindings=bindings, mutation_record=mutation,
                   instantiation_record=dict(record))
