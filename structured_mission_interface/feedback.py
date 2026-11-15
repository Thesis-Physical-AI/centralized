"""Ingest a complete execution report into a detached, conservative snapshot.

No model calls, commands, retries or automatic plan replacement. The caller must
provide the original validated dispatch and its expected execution revision.
"""
import argparse
from copy import deepcopy
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .interface import DEFAULT_FLEET, load_json, validate_world_context
from .schema import Id


class Observation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    robot_id: Id
    kind: Literal["entity_located", "entity_absent", "entity_state", "robot_state"]
    subject: Id
    value: dict
    confidence: float = Field(ge=0, le=1)
    source: Literal["executor", "perception", "vlm"]
    mission_id: Id
    step_id: Id
    event_id: Id
    stamp: Id


class Result(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    mission_id: Id
    plan_revision: int = Field(ge=0)
    step_id: Id
    robot_id: Id
    attempt: int = Field(ge=1)
    status: Literal["SUCCEEDED", "FAILED", "BLOCKED", "TIMED_OUT", "CANCELED", "REJECTED"]
    reason_code: Literal["", "UNKNOWN_ACTION", "INVALID_ARGS", "STALE_PLAN", "ROBOT_BUSY",
                         "ESTOPPED", "PRECONDITION_FAILED", "OBJECT_NOT_FOUND", "PATH_BLOCKED",
                         "EXECUTION_ERROR", "SAFETY_LIMIT", "DECISION_LIMIT", "COMMS_LOST", "TIMEOUT"]
    reason: str = ""
    observations: list[Observation] = Field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""
    decision_trace_ref: str = ""
    contract_version: str = "0.1.0"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def ingest_feedback(dispatch, report, *, plan_revision, fleet=None):
    """Recompute a snapshot from one full report; inputs are never modified.

    NOT_DISPATCHED entries are manager bookkeeping, never robot results.
    Only revision 0.1 reports and first attempts are currently supported.
    Reprocessing the same report is deterministic and does not accumulate events.
    """
    _require(type(plan_revision) is int and plan_revision >= 0, "invalid expected revision")
    _require(isinstance(report, dict), "report must be an object")
    json.dumps(report, allow_nan=False)  # Python callers must also provide finite JSON data.
    fleet = load_json(DEFAULT_FLEET) if fleet is None else fleet
    validation = validate_world_context(dispatch["world"], fleet)
    _require(validation.valid, f"invalid input world: {validation.to_dict()['errors']}")
    mission_id = dispatch["mission_id"]
    _require(dispatch.get("unassigned") == [], "dispatch contains unassigned tasks")
    execution = report.get("execution", report)
    _require(isinstance(execution, dict), "execution report must be an object")
    _require(execution["mission_id"] == mission_id, "report mission mismatch")
    _require(type(execution["plan_revision"]) is int and execution["plan_revision"] == plan_revision,
             "stale or mismatched report revision")
    if "execution" in report:
        _require(report.get("mission_id") == mission_id, "outer report mission mismatch")

    steps, dependencies, previous = {}, {}, {}
    for step in dispatch["steps"]:
        sid, rid = step["id"], step["robot"]
        _require(sid not in steps, "duplicate dispatch step")
        _require(rid in dispatch["world"]["robots"], "step robot is not active")
        steps[sid] = step
        dependencies[sid] = set(step.get("depends_on", []))
        if rid in previous:
            dependencies[sid].add(previous[rid])
        previous[rid] = sid
    _require(bool(steps), "dispatch has no steps")
    ordered, done = [], set()
    while len(done) < len(steps):
        ready = [sid for sid in steps if sid not in done and dependencies[sid] <= done]
        _require(bool(ready), "unknown or cyclic dispatch dependency")
        ordered.extend(ready)
        done.update(ready)

    results, event_ids = {}, set()
    for raw in execution["steps"]:
        sid = raw["step_id"]
        _require(sid in steps and sid not in results, "unknown or duplicate report step")
        _require(raw["robot_id"] == steps[sid]["robot"], "result robot mismatch")
        if raw["status"] == "NOT_DISPATCHED":
            _require(set(raw) <= {"step_id", "robot_id", "status", "reason"},
                     "NOT_DISPATCHED cannot contain executor feedback")
            results[sid] = deepcopy(raw)
            continue
        result = Result.model_validate(raw)
        _require((result.mission_id, result.plan_revision, result.attempt) == (mission_id, plan_revision, 1),
                 "result identity/revision/attempt mismatch")
        _require(result.contract_version.startswith("0.1."), "unsupported result version")
        _require((result.status == "SUCCEEDED") == (result.reason_code == ""), "invalid status/reason pair")
        for observation in result.observations:
            _require((observation.mission_id, observation.step_id, observation.robot_id)
                     == (mission_id, sid, result.robot_id), "observation identity mismatch")
            _require(observation.event_id not in event_ids, "duplicate observation event")
            event_ids.add(observation.event_id)
            _require(observation.subject in dispatch["world"]["entities"]
                     or observation.subject in dispatch["world"]["robots"], "unknown observation subject")
        results[sid] = result.model_dump()
    _require(results.keys() == steps.keys(), "report must cover every dispatched step")
    for sid in ordered:
        if results[sid]["status"] != "NOT_DISPATCHED":
            _require(all(results[d]["status"] == "SUCCEEDED" for d in dependencies[sid]),
                     "executed step has unsuccessful prerequisites")
    statuses = [r["status"] for r in results.values()]
    status = ("HALTED" if any(s not in {"SUCCEEDED", "NOT_DISPATCHED"} for s in statuses)
              else "SUCCEEDED" if all(s == "SUCCEEDED" for s in statuses) else "PENDING")
    _require(execution["status"] == status, "inconsistent mission execution status")

    world = deepcopy(dispatch["world"])
    world["entities"] = {k: {"kind": v} if isinstance(v, str) else v for k, v in world["entities"].items()}
    evidence, review = [], []

    def ancestors(name):
        chain = set()
        while name in world["entities"] and name not in chain:
            chain.add(name)
            name = world["entities"][name].get("in")
        return chain

    for sid in ordered:
        result, step = results[sid], steps[sid]
        if result["status"] not in {"SUCCEEDED", "NOT_DISPATCHED"}:
            review.append({"step_id": sid, "reason": "failed step may have partial physical effects; reconcile state"})
        if result["status"] == "SUCCEEDED" and step["action"] in {"movetowards", "inspect"}:
            target = step["args"]["target"]
            if any(world["entities"][p]["kind"] == "room" for p in ancestors(target)):
                robot = world["robots"][step["robot"]]
                robot["at"] = target
                robot.pop("room", None)  # derive room from the new location
            else:
                review.append({"step_id": sid, "reason": "navigation target has no unambiguous room"})
        elif result["status"] == "SUCCEEDED" and step["action"] != "search":
            review.append({"step_id": sid, "reason": "action world effects not implemented"})
        for observation in result.get("observations", []):
            evidence.append(deepcopy(observation))
            if observation["kind"] == "entity_absent":
                # A failed search does not remove an entity or invalidate its last known location.
                review.append({"event_id": observation["event_id"], "reason": "negative search evidence only"})
                continue
            location = observation["value"].get("in")
            supported = (observation["kind"] == "entity_located" and result["status"] == "SUCCEEDED"
                         and step["action"] == "search" and observation["subject"] == step["args"].get("object")
                         and location == step["args"].get("area") and location in world["entities"]
                         and world["entities"][location]["kind"] in {"room", "surface", "container"}
                         and observation["confidence"] == 1.0 and observation["source"] != "vlm")
            if not supported:
                review.append({"event_id": observation["event_id"], "reason": "observation requires review"})
                continue
            entity = world["entities"][observation["subject"]]
            current = entity.get("in")
            if current is None:
                entity["in"] = location
            elif location not in ancestors(current):
                review.append({"event_id": observation["event_id"], "reason": "conflicting location evidence"})
            # Preserve e.g. apple in table when the observation only says living_room.

    validation = validate_world_context(world, fleet)
    _require(validation.valid, f"updated world is invalid: {validation.to_dict()['errors']}")
    tasks = {}
    for step in steps.values():
        tasks.setdefault(step["task_id"], []).append(results[step["id"]]["status"])
    tasks = {tid: "COMPLETED" if all(s == "SUCCEEDED" for s in states) else
             "NEEDS_REVIEW" if any(s not in {"SUCCEEDED", "NOT_DISPATCHED"} for s in states) else
             "IN_PROGRESS" if "SUCCEEDED" in states else "NOT_STARTED" for tid, states in tasks.items()}
    return {"mission_id": mission_id, "plan_revision": plan_revision, "status": status,
            "replanning_required": status == "HALTED", "world": world, "steps": results,
            "tasks": tasks, "observations": evidence, "review": review}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dispatch", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--plan-revision", required=True, type=int)
    parser.add_argument("--fleet", default=str(DEFAULT_FLEET))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        snapshot = ingest_feedback(load_json(args.dispatch), load_json(args.report),
                                   plan_revision=args.plan_revision, fleet=load_json(args.fleet))
        payload = json.dumps(snapshot, indent=2, allow_nan=False) + "\n"
        with open(args.output, "x", encoding="utf-8") as stream:
            stream.write(payload)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"feedback ingestion failed: {exc}\n")


if __name__ == "__main__":
    main()
