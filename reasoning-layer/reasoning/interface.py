"""Minimal Mission Interface gate between reasoning and orchestration."""

import json
from copy import deepcopy
from typing import Any

from pydantic import Field

from .contracts import ContractError, MissionRequest, MissionTask, StrictModel, validate_context, validate_task_graph


class MissionHandoff(StrictModel):
    mission_id: str = Field(min_length=1, max_length=200)
    tasks: list[MissionTask] = Field(min_length=1, max_length=100)
    world: dict[str, Any]


def validate_handoff(candidate: dict, request: MissionRequest, fleet: dict) -> dict:
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
    return deepcopy(handoff.model_dump())
