"""Persistent revision ledger and activation gates for the local simulator bridge."""
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import time
import uuid

from structured_mission_interface.feedback import ingest_feedback
from structured_mission_interface import validate_world_context

sys.path.insert(0, str(Path(__file__).resolve().parent / "reasoning-layer"))
from reasoning.contracts import MissionRequest, active_fleet
from reasoning.interface import validate_handoff
from reasoning.orchestration import ActionStep, DispatchDraft, check_dispatch_contract, dispatch_validator
from reasoning.replanning import objective_ledger


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False))
    tmp.replace(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checked_checkpoint(state, mission_id, revision):
    require(isinstance(state, dict) and state.get("mission_id") == mission_id,
            "checkpoint belongs to another mission")
    require(type(state.get("active_revision")) is int and state["active_revision"] == revision
            and state.get("phase") == "IDLE", "executor must acknowledge this revision is idle")
    require(isinstance(state.get("session_id"), str) and bool(state["session_id"])
            and isinstance(state.get("quiescence_token"), str) and bool(state["quiescence_token"]),
            "missing physical session/checkpoint identity")
    point = state.get("checkpoint", {})
    pose = point.get("pose")
    require(isinstance(pose, list) and len(pose) == 3 and all(type(v) in (float, int)
            and math.isfinite(v) for v in pose), "invalid checkpoint pose")
    require(type(point.get("sim_time")) in (float, int) and math.isfinite(point["sim_time"])
            and point["sim_time"] >= 0 and isinstance(point.get("safety"), str), "invalid checkpoint telemetry")
    return deepcopy(state)


class MissionLifecycle:
    def __init__(self, folder, request, dispatch, fleet, *, objectives=None, max_revisions=3, execution_mode="map"):
        self.folder, self.fleet = Path(folder), deepcopy(fleet)
        require(type(max_revisions) is int and 0 <= max_revisions <= 10, "invalid recovery revision limit")
        require(execution_mode in {"map", "live"}, "unsupported execution mode")
        self.folder.mkdir(parents=True, exist_ok=True)
        require(not (self.folder / "lifecycle.json").exists(), "mission lifecycle already exists")
        require(request["mission_id"] == dispatch["mission_id"] and request["world"] == dispatch["world"],
                "initial dispatch must match the original request")
        ids = {s["task_id"] for s in dispatch["steps"]}
        self.data = {"mission_id": request["mission_id"], "original_request": deepcopy(request),
                     "objectives": {t["task_id"]: {k: v for k, v in t.items() if k != "task_id"}
                                    for t in objectives or [] if t["task_id"] in ids},
                     "active_revision": 0, "max_revisions": max_revisions, "status": "EXECUTING",
                     "execution_mode": execution_mode,
                     "completed_objective_ids": [], "clarifications": [], "pending": None,
                     "revisions": [{"plan_revision": 0, "request": deepcopy(request), "dispatch": deepcopy(dispatch),
                                    "objective_coverage": {i: [i] for i in ids}}]}
        self.save()

    @property
    def current(self):
        return self.data["revisions"][-1]

    def save(self):
        write_json(self.folder / "lifecycle.json", self.data)

    def history(self):
        return {"objectives": deepcopy(self.data["objectives"]),
                "revisions": [{k: deepcopy(v) for k, v in entry.items()
                               if k in {"plan_revision", "dispatch", "report", "objective_coverage"}}
                              for entry in self.data["revisions"][:-1]],
                "source_objective_coverage": deepcopy(self.current["objective_coverage"]),
                "reconciled_world": deepcopy(self.current.get("world")),
                "reconciliation": deepcopy(self.current.get("reconciliation")),
                "activation_feedback": deepcopy(self.data.get("recovery_checks", []))}

    def record_report(self, report):
        revision = report.get("execution", report).get("plan_revision")
        require(type(revision) is int and 0 <= revision <= self.data["active_revision"], "unexpected report revision")
        entry = self.data["revisions"][revision]
        feedback = ingest_feedback(entry["dispatch"], report, plan_revision=revision, fleet=self.fleet)
        if revision != self.data["active_revision"]:
            write_json(self.folder / "late" / (uuid.uuid4().hex + ".json"), report)
            return None  # validated and archived; never overwrite the active world
        if "report" in entry:
            require(entry["report"] == report, "conflicting duplicate report")
            return deepcopy(entry["feedback"])
        checkpoint = checked_checkpoint(report.get("checkpoint"), self.data["mission_id"], revision)
        if revision:
            require(checkpoint["session_id"] == self.data["revisions"][0]["checkpoint"]["session_id"],
                    "the simulator session was reset")
        entry.update(report=deepcopy(report), feedback=feedback, checkpoint=checkpoint, world=deepcopy(feedback["world"]))
        ledger = objective_ledger(entry["dispatch"], feedback, self.fleet, history=self.history())
        self.data["completed_objective_ids"] = ledger["completed_objective_ids"]
        self.data["status"] = "SUCCEEDED" if feedback["status"] == "SUCCEEDED" else "WAITING_REPLAN"
        self.save()
        return deepcopy(feedback)

    def reconcile_pose(self, scene):
        """Use the paused simulator's measured pose, never reset it to a symbolic start."""
        entry = self.current
        state = entry["checkpoint"]
        require(not state["checkpoint"]["safety"], "robot safety check requires operator intervention")
        rooms = [name for name, e in entry["world"]["entities"].items() if e["kind"] == "room"]
        require(len(rooms) == 1, "automatic pose reconciliation currently requires a single-room scene")
        sx, sy = scene["room"]["size"]
        x, y, _ = state["checkpoint"]["pose"]
        require(abs(x) < sx / 2 and abs(y) < sy / 2, "robot is outside the known room; operator reconciliation required")
        rid = scene["robot"]["id"]
        robot = entry["world"]["robots"][rid]
        approach = scene["entities"].get(robot["at"], {}).get("approach")
        if not approach or math.hypot(x - approach[0], y - approach[1]) > .25:
            robot["at"] = rooms[0]
        robot.pop("room", None)
        require(validate_world_context(entry["world"], self.fleet).valid, "reconciled world is inconsistent")
        entry["reconciliation"] = {"source": "paused_simulator_pose", "checkpoint": deepcopy(state),
                                   "robot_id": rid, "at": robot["at"],
                                   "waypoints": {name: deepcopy(e["approach"]) for name, e in scene["entities"].items()
                                                 if e.get("approach")}}
        self.save()

    def add_clarification(self, text):
        require(isinstance(text, str) and 0 < len(text.strip()) <= 5000, "provide a clarification under 5000 characters")
        require(self.data["status"] in {"WAITING_REPLAN", "REVIEW"}, "mission is not waiting for clarification")
        require(len(self.data["clarifications"]) < 5, "clarification limit reached; stop and review this mission")
        require(sum(len(c["text"]) + 1 for c in self.data["clarifications"]) + len(text.strip()) <= 5000,
                "clarification history exceeds 5000 characters; stop and review this mission")
        self.data["clarifications"].append({"source_revision": self.data["active_revision"], "text": text.strip()})
        self.save()

    def offer(self, proposal, dispatch):
        require(self.data["status"] in {"WAITING_REPLAN", "REVIEW"}, "mission is not idle for replanning")
        next_revision = self.data["active_revision"] + 1
        require(proposal.get("mission_id") == self.data["mission_id"]
                and proposal.get("source_plan_revision") == self.data["active_revision"]
                and proposal.get("proposed_plan_revision") == next_revision
                and proposal.get("status") == "ready", "invalid or stale recovery proposal")
        request = MissionRequest(mission_id=self.data["mission_id"],
                                 natural_language=self.data["original_request"]["natural_language"], world=self.current["world"])
        handoff = validate_handoff(proposal["handoff"], request, self.fleet)
        coverage = proposal.get("objective_coverage", {})
        tasks = {t["task_id"] for t in handoff["tasks"]}
        original = {s["task_id"] for s in self.data["revisions"][0]["dispatch"]["steps"]}
        remaining = original - set(self.data["completed_objective_ids"])
        require(set(coverage) == tasks and all(isinstance(refs, list) and refs
                and set(refs) <= remaining and len(set(refs)) == len(refs) for refs in coverage.values())
                and {ref for refs in coverage.values() for ref in refs} == remaining,
                "recovery must map every new task to all remaining objectives and no completed ones")
        require(all(t.startswith(f"R{next_revision}_") for t in tasks), "replacement task IDs need the new revision prefix")
        require(dispatch.get("mission_id") == self.data["mission_id"] and dispatch.get("world") == request.world,
                "replacement dispatch must use the reconciled world")
        draft = DispatchDraft.model_validate({"allocations": dispatch["allocations"], "unassigned": dispatch["unassigned"],
            "steps": [{k: v for k, v in s.items() if k in ActionStep.model_fields} for s in dispatch["steps"]]})
        canonical = check_dispatch_contract(draft, handoff, active_fleet(self.fleet, request.world))
        errors, warnings = dispatch_validator().validate(deepcopy(canonical), self.fleet)
        require(not errors and not warnings and not canonical["unassigned"], f"replacement validation failed: {errors + warnings}")
        for objective in remaining:
            rules = self.data["objectives"].get(objective, {}).get("constraints", {})
            mapped = {t for t, refs in coverage.items() if objective in refs}
            steps = [s for s in canonical["steps"] if s["task_id"] in mapped]
            original_steps = [s for s in self.data["revisions"][0]["dispatch"]["steps"] if s["task_id"] == objective]
            goal_actions = {"search", "inspect"} if any(s["action"] in {"search", "inspect"}
                                                        for s in original_steps) else {"movetowards"}
            def predicate(s):
                return s["action"], s["args"].get("object" if s["action"] == "search" else "target")
            required = {predicate(s) for s in original_steps if s["action"] in goal_actions}
            verified = set()
            for entry in self.data["revisions"]:
                if "feedback" not in entry:
                    continue
                related = {t for t, refs in entry["objective_coverage"].items() if objective in refs}
                verified.update(predicate(s) for s in entry["dispatch"]["steps"] if s["task_id"] in related
                                and s["action"] in goal_actions and entry["feedback"]["steps"][s["id"]]["status"] == "SUCCEEDED")
            proposed = {predicate(s) for s in steps if s["action"] in goal_actions}
            require(required - verified <= proposed, "replacement drops an unfinished search/inspection/navigation objective")
            require(not verified & proposed, "replacement repeats an already verified objective predicate")
            require(set(rules.get("required_actions", [])) <= {s["action"] for s in steps},
                    "replacement drops an original required action")
            if "allowed_robots" in rules:
                require(all(s["robot"] in rules["allowed_robots"] for s in steps),
                        "replacement violates an original robot restriction")
            if "payload_kg" in rules:
                capacities = {r["id"]: r["max_payload_kg"] for r in self.fleet["robots"]}
                require(any(s["action"] == "grab" and capacities[s["robot"]] >= rules["payload_kg"] for s in steps),
                        "replacement drops an original payload requirement")
        require(all(s["robot"] == "puppypi_1" and s["action"] in {"movetowards", "inspect", "search"}
                    for s in canonical["steps"]), "this simulator session lacks an executor for replacement actions/robots")
        if self.data["execution_mode"] == "map":
            approaches = self.current.get("reconciliation", {}).get("waypoints", {})
            require(all(s["args"].get("target") in approaches for s in canonical["steps"]
                        if s["action"] in {"movetowards", "inspect"}), "replacement target has no map approach pose")
        self.data["pending"] = {"revision": next_revision, "proposal": deepcopy(proposal), "dispatch": canonical,
                                "objective_coverage": deepcopy(coverage)}
        self.data["status"] = "REVIEW"
        self.save()
        return self.activation_reasons()

    def activation_reasons(self, *, acknowledge_review=False):
        reasons = []
        pending, entry = self.data["pending"], self.current
        if not pending:
            return ["no validated replacement dispatch"]
        if pending["revision"] > self.data["max_revisions"]:
            reasons.append("automatic recovery limit reached")
        if not entry.get("reconciliation"):
            reasons.append("physical state has not been reconciled")
        for item in entry["feedback"]["review"]:
            if item["reason"] == "negative search evidence only":
                continue
            if "partial physical effects" in item["reason"] and entry.get("reconciliation"):
                step = next(s for s in entry["dispatch"]["steps"] if s["id"] == item["step_id"])
                if step["action"] in {"search", "movetowards", "inspect"}:
                    continue
            if not acknowledge_review:
                reasons.append("unresolved observation/state review requires operator confirmation")
        failures = [(past, s) for past in self.data["revisions"] if "feedback" in past
                    for s in past["dispatch"]["steps"]
                    if past["feedback"]["steps"][s["id"]]["status"] not in {"SUCCEEDED", "NOT_DISPATCHED"}]
        new_steps = pending["dispatch"]["steps"]
        waypoints = entry.get("reconciliation", {}).get("waypoints", {})
        x, y, _ = entry["checkpoint"]["checkpoint"]["pose"]
        def different_view(target, subject):
            point = waypoints.get(target)
            attempted = [past["checkpoint"]["checkpoint"]["pose"] for past, s in failures
                         if s["action"] == "search" and s["args"].get("object") == subject
                         and past["feedback"]["steps"][s["id"]]["reason_code"] == "OBJECT_NOT_FOUND"]
            return point is not None and math.hypot(point[0] - x, point[1] - y) >= .3 and all(
                math.hypot(point[0] - p[0], point[1] - p[1]) >= .3 for p in attempted)
        for _, old in failures:
            for index, step in enumerate(new_steps):
                last_target = None
                for earlier in new_steps[:index]:
                    if earlier["robot"] == step["robot"]:
                        target = earlier["args"].get("area") if earlier["action"] == "search" else earlier["args"].get("target")
                        if target in waypoints:
                            last_target = target
                area = step["args"].get("area")
                changed_view = step["action"] == "search" and different_view(
                    area if area in waypoints else last_target, step["args"].get("object"))
                if (old["action"] == step["action"] == "search" and old["args"].get("object") == step["args"].get("object")
                        and old["args"].get("area") != step["args"].get("area")
                        and not changed_view):
                    reasons.append("changed search area does not provide a new reachable viewpoint")
                if (step["action"], step["args"]) != (old["action"], old["args"]):
                    continue
                if not (old["action"] == "search" and changed_view):
                    reasons.append("unchanged failed action would be retried without a different supported vantage")
        return sorted(set(reasons))

    def begin_activation(self, checkpoint, *, pause_ack=None, acknowledge_review=False):
        reasons = self.activation_reasons(acknowledge_review=acknowledge_review)
        require(not reasons, "; ".join(reasons))
        live = checked_checkpoint(checkpoint, self.data["mission_id"], self.data["active_revision"])
        require(isinstance(pause_ack, dict) and pause_ack.get("accepted") is True
                and bool(pause_ack.get("command_id")) and all(pause_ack.get(k) == live[k] for k in live),
                "a matching fresh pause acknowledgment is required")
        old = self.current["checkpoint"]
        require(live["session_id"] == old["session_id"] and live["checkpoint"] == old["checkpoint"],
                "physical state changed after planning; revalidate before activation")
        self.current["checkpoint"] = deepcopy(live)
        self.current["reconciliation"]["checkpoint"] = deepcopy(live)
        command = {"command_id": uuid.uuid4().hex, "action": "activate", "mission_id": live["mission_id"],
                   "session_id": live["session_id"], "expected_revision": live["active_revision"],
                   "quiescence_token": live["quiescence_token"], "revision": self.data["pending"]["revision"],
                   "dispatch": deepcopy(self.data["pending"]["dispatch"])}
        self.data["pending"]["command"] = command
        self.data["pending"]["operator_review_confirmed"] = bool(acknowledge_review)
        self.data["status"] = "ACTIVATING"
        self.save()
        return deepcopy(command)

    def commit_activation(self, ack):
        pending = self.data["pending"]
        require(self.data["status"] == "ACTIVATING" and pending, "no activation is pending")
        command = pending["command"]
        require(ack.get("accepted") is True and ack.get("command_id") == command["command_id"]
                and ack.get("mission_id") == command["mission_id"] and ack.get("session_id") == command["session_id"]
                and ack.get("active_revision") == command["revision"] and ack.get("phase") == "RUNNING",
                "replacement activation was not acknowledged by this physical session")
        self.data["revisions"].append({"plan_revision": pending["revision"],
            "request": {**deepcopy(self.data["original_request"]), "world": deepcopy(pending["dispatch"]["world"])},
            "dispatch": deepcopy(pending["dispatch"]), "world": deepcopy(pending["dispatch"]["world"]),
            "objective_coverage": deepcopy(pending["objective_coverage"]),
            "proposal": deepcopy(pending["proposal"]),
            "activation_decision": {"source_revision": self.data["active_revision"],
                                    "operator_review_confirmed": pending["operator_review_confirmed"],
                                    "review_status": "operator_confirmed" if pending["operator_review_confirmed"] else "automatic_gates_passed",
                                    "command_id": command["command_id"]},
            "activation_ack": deepcopy(ack)})
        self.data.update(active_revision=pending["revision"], pending=None, status="EXECUTING")
        self.save()


class SessionClient:
    def __init__(self, folder, *, check_cancel=lambda: None, alive=lambda: True):
        self.root = Path(folder)
        self.check_cancel, self.alive = check_cancel, alive

    def wait(self, relative, timeout=900):
        deadline = time.monotonic() + timeout
        path = self.root / relative
        while True:
            self.check_cancel()
            if path.exists():
                return json.loads(path.read_text())
            require(self.alive(), "simulator session ended unexpectedly")
            if time.monotonic() > deadline:
                raise TimeoutError(f"simulator did not publish {relative}")
            time.sleep(.1)

    def checkpoint(self):
        require(self.alive(), "simulator session is no longer alive")
        return self.wait("status.json", 30)

    def send(self, command):
        self.check_cancel()
        require(self.alive(), "cannot send a command to a closed simulator session")
        target = self.root / "commands" / (command["command_id"] + ".json")
        require(not target.exists(), "command already submitted; inspect its existing acknowledgment")
        write_json(target, command)
        ack = self.wait("acks/" + target.name, 30)
        require(ack.get("accepted"), "simulator rejected activation: " + str(ack.get("error", "unknown error")))
        return ack
