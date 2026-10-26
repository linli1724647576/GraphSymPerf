"""M4 materializes the accepted source and declared alternative terminals."""

from copy import deepcopy

import ir
from symbolic import _resolve, prepare_instantiation

from .contracts import InstantiatedQuery, StageFailure, digest
from .state_builder import validate_state_and_plan


def instantiate_query(embedded, graph_state, binding_plan):
    skeleton = validate_state_and_plan(embedded, graph_state, binding_plan)
    try:
        prepared = prepare_instantiation(skeleton, graph_state.solved)
    except ValueError as error:
        raise StageFailure("M4", "contract_drift", str(error)) from error
    source = prepared.source
    replacement = _resolve(skeleton.replacement, graph_state.solved.assignment)
    # _resolve only accepts scalar terminals. All structure holes were rejected
    # before construction, so neither this stage nor its helper can grow ASTs.
    ir.validate_query(source)
    record = {"algorithm": 2, "method_stage": "M4", "stage": "instantiate_source",
              "embedded_digest": embedded.embedded_digest,
              "graph_digest": graph_state.graph_digest,
              "binding_plan_digest": binding_plan.plan_digest,
              "uses_construction_assignment": graph_state.solved.construction_record["assignment_digest"],
              "skeleton_digest": graph_state.solved.construction_record["skeleton_digest"],
              "source_digest": digest(source), "replacement_digest": digest(replacement),
              "bindings_digest": digest(prepared.bindings), "checks_digest": digest(prepared.checks),
              "unchanged_graph": True, "structure_unchanged": True,
              "marked_site": list(prepared.site.address), "target_id": embedded.target_id}
    return InstantiatedQuery(source, replacement, deepcopy(prepared.bindings),
                             tuple(deepcopy(prepared.checks)), record, prepared,
                             embedded, graph_state, binding_plan)
