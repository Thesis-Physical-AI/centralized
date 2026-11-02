"""Bridge the shared Structured Mission Interface into the Gemini pipeline."""

import json
from copy import deepcopy
from typing import Any

from pydantic import Field

from .contracts import ContractError, MissionRequest, MissionTask, StrictModel, validate_context, validate_task_graph
from structured_mission_interface import validate_mission


class MissionHandoff(StrictModel):
    mission_id: str = Field(min_length=1, max_length=200)
    tasks: list[MissionTask] = Field(min_length=1, max_length=100)
    world: dict[str, Any]


def validate_handoff(candidate: dict, request: MissionRequest, fleet: dict,
                     *, warnings: list[dict] | None = None) -> dict:
    """Recheck task structure and bind a handoff to the original mission snapshot.

    This is a structural gate, not natural-language goal verification or the
    complete policy/approval interface described in the research documents.
    """
    handoff = MissionHandoff.model_validate(candidate)
    if handoff.mission_id != request.mission_id:
        raise ContractError("Handoff mission_id does not match the request.")
    supplied = json.dumps(handoff.world, sort_keys=True, allow_nan=False)
    original = json.dumps(request.world, sort_keys=True, allow_nan=False)
    if supplied != original:
        raise ContractError("Handoff world does not match the request snapshot.")
    validate_context(request, fleet)
    validate_task_graph(handoff.tasks)
    result = validate_mission(handoff.model_dump(), fleet)
    if not result.valid:
        raise ContractError("Structured Mission Interface rejected the candidate: " + "; ".join(
            f"{issue.path}: {issue.message}" for issue in result.errors))
    if warnings is not None:
        warnings.extend(issue.to_dict() for issue in result.warnings)
    accepted = deepcopy(result.mission)
    # The boundary validates a normalized copy but cannot rewrite the planner's
    # authoritative snapshot or drop its metadata.
    accepted["world"] = deepcopy(handoff.world)
    return accepted
