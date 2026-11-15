"""Trusted preparation of reasoning context from original dispatch and feedback."""
from copy import deepcopy
import json

from structured_mission_interface.feedback import ingest_feedback

from .contracts import ContractError, MissionRequest, validate_context


def prepare_replanning(request, fleet, *, dispatch, report, plan_revision):
    validate_context(request, fleet)
    if not isinstance(dispatch, dict) or not isinstance(report, dict):
        raise ContractError("Dispatch and report must be JSON objects")
    if dispatch.get("mission_id") != request.mission_id:
        raise ContractError("Dispatch does not belong to the original mission request")
    if json.dumps(dispatch["world"], sort_keys=True, allow_nan=False) != json.dumps(
            request.world, sort_keys=True, allow_nan=False):
        raise ContractError("Dispatch world does not match the original request snapshot")
    snapshot = ingest_feedback(dispatch, report, plan_revision=plan_revision, fleet=fleet)
    if not snapshot["replanning_required"]:
        raise ContractError("Replanning requires a halted execution report")
    context = {
        "source_plan_revision": plan_revision,
        "proposed_plan_revision": plan_revision + 1,
        "execution_status": snapshot["status"],
        "task_execution_status": snapshot["tasks"],
        "completed_task_ids": [tid for tid, status in snapshot["tasks"].items() if status == "COMPLETED"],
        "previous_steps": deepcopy(dispatch["steps"]),
        "step_results": snapshot["steps"],
        "observations": snapshot["observations"],
        "review": snapshot["review"],
    }
    updated = MissionRequest(mission_id=request.mission_id, natural_language=request.natural_language,
                             world=deepcopy(snapshot["world"]))
    return updated, context
