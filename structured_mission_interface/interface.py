"""Schema and cross-reference checks without allocating or executing tasks."""
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path

from pydantic import ValidationError

from .schema import FLEET_ADAPTER, MISSION_ADAPTER, WORLD_ADAPTER

DEFAULT_FLEET = Path(__file__).resolve().parents[1] / "mission-orchestrator" / "robots.json"


@dataclass(frozen=True)
class Issue:
    path: str
    code: str
    message: str

    def to_dict(self):
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple
    warnings: tuple = ()
    mission: dict | None = None

    @property
    def valid(self):
        return not self.errors

    def to_dict(self):
        return {"valid": self.valid, "errors": [e.to_dict() for e in self.errors],
                "warnings": [w.to_dict() for w in self.warnings], "mission": deepcopy(self.mission)}


class MissionValidationError(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__("; ".join(f"{e.path}: {e.message}" for e in result.errors))


def _path(parts):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _schema_errors(value, adapter, prefix=()):
    try:
        adapter.validate_python(value)
    except ValidationError as exc:
        return [Issue(_path((*prefix, *item["loc"])), "schema", item["msg"])
                for item in exc.errors(include_input=False, include_url=False)]
    return []


def _json_errors(value, parts=()):
    """Python callers must satisfy JSON's finite-number and string-key rules too."""
    if isinstance(value, float) and not math.isfinite(value):
        return [Issue(_path(parts), "invalid_json", "numbers must be finite")]
    if isinstance(value, dict):
        errors = []
        for key, item in value.items():
            if not isinstance(key, str):
                errors.append(Issue(_path(parts), "invalid_json", "object keys must be strings"))
            errors.extend(_json_errors(item, (*parts, key)))
        return errors
    if isinstance(value, list):
        return [e for i, item in enumerate(value) for e in _json_errors(item, (*parts, i))]
    if value is not None and not isinstance(value, (str, bool, int, float)):
        return [Issue(_path(parts), "invalid_json", "value is not a JSON type")]
    return []


def validate_world_context(world, fleet):
    """Validate observations and registry before any model call, without tasks."""
    errors = []
    for value, adapter, prefix in ((world, WORLD_ADAPTER, ("world",)),
                                   (fleet, FLEET_ADAPTER, ("fleet",))):
        errors.extend(_json_errors(value, prefix) or _schema_errors(value, adapter, prefix))
    if not errors:
        normalized = deepcopy(world)
        normalized.setdefault("open", [])
        normalized.setdefault("passages", [])
        normalized["entities"] = {name: {"kind": entity} if isinstance(entity, str) else entity
                                  for name, entity in normalized["entities"].items()}
        errors.extend(_world_errors(normalized, fleet))
    return ValidationResult(tuple(errors))


def _world_errors(world, fleet):
    errors = []

    def error(path, code, message):
        errors.append(Issue(path, code, message))

    robots = {}
    for i, robot in enumerate(fleet["robots"]):
        rid = robot["id"]
        if rid in robots:
            error(f"/fleet/robots/{i}/id", "duplicate_id", f"duplicate robot '{rid}'")
        robots[rid] = robot
    entities, states = world["entities"], world["robots"]
    for name in entities.keys() & robots.keys():
        error(f"/world/entities/{name}", "id_collision", "entity id collides with fleet robot id")
    for rid in states.keys() - robots.keys():
        error(f"/world/robots/{rid}", "unknown_robot", "robot is not in the fleet registry")

    for name, entity in entities.items():
        path = "/world/entities/" + name.replace("~", "~0").replace("/", "~1")
        parent = entity.get("in")
        if parent is not None:
            if parent not in entities and parent not in states:
                error(path + "/in", "unknown_reference", f"unknown or inactive parent '{parent}'")
            elif parent in entities and entities[parent]["kind"] not in {"room", "surface", "container"}:
                error(path + "/in", "invalid_parent", "parent must be a room, surface, container or active robot")
        if entity["kind"] == "room" and parent is not None:
            error(path + "/in", "invalid_room", "rooms cannot have a parent")
        if entity["kind"] == "door":
            if "connects" not in entity:
                error(path + "/connects", "missing_connections", "doors must connect two known rooms")
            for room in entity.get("connects", []):
                if entities.get(room, {}).get("kind") != "room":
                    error(path + "/connects", "invalid_room", f"'{room}' is not a known room")
        elif "connects" in entity:
            error(path + "/connects", "invalid_connections", "only doors have connects")

    # Check the location graph including robots, so a robot on its own tray
    # and an object supporting the robot carrying it cannot form a hidden cycle.
    parents = {name: e.get("in") for name, e in entities.items()}
    parents.update({rid: st["at"] for rid, st in states.items()})
    finished = set()
    for start in parents:
        chain, seen = [], set()
        node = start
        while node in parents and node not in finished:
            if node in seen:
                error("/world", "location_cycle", "location cycle: " + " -> ".join(chain + [node]))
                break
            seen.add(node)
            chain.append(node)
            node = parents[node]
        finished.update(chain)

    def room_of(name):
        seen = set()
        while name is not None and name not in seen:
            seen.add(name)
            if entities.get(name, {}).get("kind") == "room":
                return name
            if name in states:
                state = states[name]
                if state.get("room"):
                    return state["room"]
                name = state["at"]
            else:
                name = entities.get(name, {}).get("in")
        return None

    for i, name in enumerate(world["open"]):
        if entities.get(name, {}).get("kind") not in {"door", "container"}:
            error(f"/world/open/{i}", "invalid_open", f"'{name}' is not a known door or container")
    for i, passage in enumerate(world["passages"]):
        if any(entities.get(room, {}).get("kind") != "room" for room in passage):
            error(f"/world/passages/{i}", "invalid_passage", "passages must connect known rooms")

    holders = {}
    for rid, state in states.items():
        path = "/world/robots/" + rid
        at = state["at"]
        if at not in entities and at not in states:
            error(path + "/at", "unknown_reference", f"unknown robot location '{at}'")
        explicit_room = state.get("room")
        if explicit_room is not None and entities.get(explicit_room, {}).get("kind") != "room":
            error(path + "/room", "invalid_room", "room must reference a known room")
        location_room = room_of(at)
        if explicit_room and location_room and explicit_room != location_room:
            error(path + "/room", "inconsistent_location", "room disagrees with the robot location")
        if not location_room and not explicit_room:
            error(path + "/at", "unresolved_location", "robot location must resolve to a room; provide room when at a door")
        if entities.get(at, {}).get("kind") == "door" and explicit_room not in entities[at].get("connects", []):
            error(path + "/room", "invalid_door_side", "robot at a door must specify one connected room")
        robot = robots.get(rid)
        if robot and state.get("airborne") and "takeoff_from" not in robot["actions"]:
            error(path + "/airborne", "unsupported_flight", "robot has no flight capability")
        held = state.get("holding")
        if held:
            if held in holders:
                error(path + "/holding", "multiple_holders", f"'{held}' is already held by {holders[held]}")
            holders[held] = rid
            entity = entities.get(held, {})
            if entity.get("kind") != "object" or entity.get("in") != rid:
                error(path + "/holding", "invalid_holding", "held object must exist and have in equal to the holding robot")
            if robot and "grab" not in robot["actions"]:
                error(path + "/holding", "unsupported_grasp", "robot cannot grab objects")
            if robot and entity.get("weight_kg", 0) > robot["max_payload_kg"]:
                error(path + "/holding", "payload_exceeded", "held object exceeds robot payload")
    for name, entity in entities.items():
        parent = entity.get("in")
        if parent in states and parent in robots and states[parent].get("holding") != name and not robots[parent]["cargo"]:
            error("/world/entities/" + name + "/in", "unsupported_cargo", "object on a robot must be held or supported by a cargo tray")

    return errors


def validate_mission(candidate, fleet=None):
    """Return errors or a detached, normalized orchestrator input.

    Candidate and fleet are JSON-compatible objects. Omitted fleet loads the
    repository registry; only robots present in world.robots are active.
    """
    errors, warnings = [], []

    def error(path, code, message):
        errors.append(Issue(path, code, message))

    if fleet is None:
        try:
            fleet = load_json(DEFAULT_FLEET)
        except (OSError, ValueError) as exc:
            return ValidationResult((Issue("/fleet", "fleet_load", str(exc)),))
    for value, schema, prefix in ((candidate, MISSION_ADAPTER, ()), (fleet, FLEET_ADAPTER, ("fleet",))):
        json_errors = _json_errors(value, prefix)
        errors.extend(json_errors or _schema_errors(value, schema, prefix))
    if errors:
        return ValidationResult(tuple(errors))

    mission = deepcopy(candidate)
    world = mission["world"]
    world.setdefault("open", [])
    world.setdefault("passages", [])
    world["entities"] = {name: {"kind": entity} if isinstance(entity, str) else entity
                         for name, entity in world["entities"].items()}
    errors.extend(_world_errors(world, fleet))

    robots = {robot["id"]: robot for robot in fleet["robots"]}
    states = world["robots"]
    tasks, dependencies = {}, {}
    for i, task in enumerate(mission["tasks"]):
        path = f"/tasks/{i}"
        tid = task["task_id"]
        if tid in tasks:
            error(path + "/task_id", "duplicate_id", f"duplicate task '{tid}'")
        tasks[tid] = task
        task.setdefault("depends_on", [])
        task.setdefault("constraints", {})
        dependencies[tid] = set(task["depends_on"])
        constraints = task["constraints"]
        if constraints.get("requirements"):
            warnings.append(Issue(path + "/constraints/requirements", "semantic_review",
                                  "text requirements are preserved for orchestration; their meaning is not machine-verified"))
        allowed = constraints.get("allowed_robots", list(states))
        for rid in allowed:
            if rid not in states or rid not in robots:
                error(path + "/constraints/allowed_robots", "unavailable_robot", f"'{rid}' is not an active registered robot")
        eligible = [robots[rid] for rid in allowed if rid in states and rid in robots]
        required = constraints.get("required_actions", [])
        for action in required:
            if not any(action in r["actions"] for r in eligible):
                error(path + "/constraints/required_actions", "unsupported_action", f"no eligible robot supports '{action}'")
        if "payload_kg" in constraints:
            payload = constraints["payload_kg"]
            if not any("grab" in r["actions"] and r["max_payload_kg"] >= payload for r in eligible):
                error(path + "/constraints/payload_kg", "payload_exceeded", "no eligible handler can lift the required payload")
        if not required:
            warnings.append(Issue(path + "/constraints", "semantic_review", "no explicit action requirements; task feasibility still requires orchestrator review"))

    for i, task in enumerate(mission["tasks"]):
        for dep in task["depends_on"]:
            if dep not in tasks:
                error(f"/tasks/{i}/depends_on", "unknown_dependency", f"unknown task '{dep}'")
    # Kahn's algorithm; keep original task order in the handoff.
    remaining = {tid: deps & tasks.keys() for tid, deps in dependencies.items()}
    while remaining:
        ready = {tid for tid, deps in remaining.items() if not deps}
        if not ready:
            error("/tasks", "dependency_cycle", "dependency cycle involving: " + ", ".join(remaining))
            break
        remaining = {tid: deps - ready for tid, deps in remaining.items() if tid not in ready}
    return ValidationResult(tuple(errors), tuple(warnings), None if errors else mission)


def prepare_mission(candidate, fleet=None):
    """Return only accepted handoff input; reject invalid missions by exception."""
    result = validate_mission(candidate, fleet)
    if not result.valid:
        raise MissionValidationError(result)
    return result.mission


def load_json(path):
    """Reject ambiguous duplicate keys and non-standard JSON numbers."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    with open(path, encoding="utf-8-sig") as stream:
        return json.load(stream, object_pairs_hook=pairs, parse_constant=constant)
