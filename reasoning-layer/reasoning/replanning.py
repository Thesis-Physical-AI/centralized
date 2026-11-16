"""Trusted preparation of reasoning context from original dispatch and feedback."""
from copy import deepcopy
import json

from structured_mission_interface.feedback import ingest_feedback

from .contracts import ContractError, MissionRequest, validate_context


def objective_ledger(dispatch, snapshot, fleet, *, history=None):
    """Revalidate earlier reports and retain stable objective IDs across revisions."""
    history = history or {}
    earlier = history.get("revisions", [])
    root = earlier[0]["dispatch"] if earlier else dispatch
    objectives = {step["task_id"] for step in root["steps"]}
    descriptions = history.get("objectives", {})
    completed, previous_revision, seen_events, summaries = set(), None, set(), []
    entries = [*earlier, {"dispatch": dispatch, "feedback": snapshot,
                         "objective_coverage": history.get("source_objective_coverage")}]
    for index, entry in enumerate(entries):
        plan = entry["dispatch"]
        if plan["mission_id"] != dispatch["mission_id"]:
            raise ContractError("History contains another mission")
        feedback = (snapshot if index == len(entries) - 1 else
                    ingest_feedback(plan, entry["report"], plan_revision=entry["plan_revision"], fleet=fleet))
        revision = feedback["plan_revision"]
        if previous_revision is not None and revision != previous_revision + 1:
            raise ContractError("History revisions must be consecutive")
        previous_revision = revision
        if index < len(entries) - 1:
            summaries.append({"plan_revision": revision, "tasks": feedback["tasks"],
                              "steps": [{"action": s["action"], "args": s["args"],
                                         "result": feedback["steps"][s["id"]]} for s in plan["steps"]]})
        events = {o["event_id"] for o in feedback["observations"]}
        if seen_events & events:
            raise ContractError("Observation event reused across revisions")
        seen_events.update(events)
        tasks = set(feedback["tasks"])
        coverage = entry.get("objective_coverage") or ({t: [t] for t in tasks} if index == 0 else {})
        if (set(coverage) != tasks or any(not isinstance(refs, list) or not refs
                or len(set(refs)) != len(refs) or set(refs) - (objectives - completed)
                for refs in coverage.values())
                or {ref for refs in coverage.values() for ref in refs} != objectives - completed):
            raise ContractError("Revision history must cover every remaining objective and no completed ones")
        for objective in objectives - completed:
            mapped = [t for t, refs in coverage.items() if objective in refs]
            if mapped and all(feedback["tasks"][t] == "COMPLETED" for t in mapped):
                completed.add(objective)
    return {"completed_objective_ids": sorted(completed),
            "remaining_objectives": [{"objective_id": oid, **descriptions.get(oid, {})}
                                      for oid in sorted(objectives - completed)],
            "previous_revisions": summaries}


def prepare_replanning(request, fleet, *, dispatch, report, plan_revision, history=None, clarification=""):
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
    if not isinstance(clarification, str) or len(clarification) > 5000:
        raise ContractError("Clarification must be text under 5000 characters")
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
        **objective_ledger(dispatch, snapshot, fleet, history=history),
        "clarification": clarification,
        "activation_feedback": deepcopy((history or {}).get("activation_feedback", [])),
    }
    world = deepcopy(snapshot["world"])
    if history and history.get("reconciled_world") is not None:
        from structured_mission_interface import validate_world_context
        corrected = deepcopy(history["reconciled_world"])
        if not validate_world_context(corrected, fleet).valid:
            raise ContractError("Reconciled world is invalid")
        comparison = deepcopy(corrected)
        for rid, state in comparison["robots"].items():
            original = world["robots"][rid]
            for key in ("at", "room"):
                state.pop(key, None)
                if key in original:
                    state[key] = original[key]
        if comparison != world:
            raise ContractError("Pose reconciliation may only update robot location, not entities or other state")
        world = corrected
        context["reconciliation"] = deepcopy(history.get("reconciliation"))
    updated = MissionRequest(mission_id=request.mission_id, natural_language=request.natural_language, world=world)
    return updated, context
