"""Typed boundaries and task-graph checks; no robot action generation."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractError(ValueError):
    """Input or generated tasks do not satisfy the planning boundary."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MissionRequest(StrictModel):
    mission_id: str = Field(min_length=1, max_length=200)
    natural_language: str = Field(min_length=1, max_length=20000)
    world: dict[str, Any]

    @model_validator(mode="after")
    def meaningful_strings(self):
        if not self.mission_id.strip() or not self.natural_language.strip():
            raise ValueError("mission_id and natural_language cannot be blank")
        return self


class MissionTask(StrictModel):
    task_id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=4000)
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def meaningful_strings(self):
        if not self.task_id.strip() or not self.description.strip():
            raise ValueError("task_id and description cannot be blank")
        return self


class ModelDecision(StrictModel):
    status: Literal["ready", "needs_clarification", "infeasible"]
    tasks: list[MissionTask] = Field(default_factory=list, max_length=100)
    questions: list[str] = Field(default_factory=list, max_length=20)
    reasons: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def consistent_decision(self):
        if any(not text.strip() for text in self.questions + self.reasons):
            raise ValueError("questions and reasons cannot contain blank strings")
        if self.status == "ready":
            if not self.tasks or self.questions or self.reasons:
                raise ValueError("ready requires tasks and empty questions/reasons")
            validate_task_graph(self.tasks)
        else:
            if self.tasks:
                raise ValueError("a non-ready decision cannot contain dispatchable tasks")
            if self.status == "needs_clarification" and not self.questions:
                raise ValueError("needs_clarification requires at least one question")
            if self.status == "infeasible" and (not self.reasons or self.questions):
                raise ValueError("infeasible requires reasons and no questions")
        return self


def generation_schema() -> dict[str, Any]:
    """Use a compact schema in the prompt, with strict contract checks locally.

    Inline references and enumerate a small constraints vocabulary instead of
    asking Gemini to generate arbitrary JSON object keys. The orchestrator
    still receives a constraints object; its requirements remain plain language.
    """
    return compact_schema(ModelDecision)


def compact_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Inline model references and omit nonessential prompt-schema metadata."""
    source = model.model_json_schema()
    definitions = source.get("$defs", {})

    def portable(node):
        if isinstance(node, list):
            return [portable(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return portable(definitions[node["$ref"].rsplit("/", 1)[1]])
        result = {key: portable(value) for key, value in node.items()
                  if key not in {"$defs", "additionalProperties", "default", "title", "minLength", "maxLength"}}
        if "constraints" in result.get("properties", {}):
            result["properties"]["constraints"] = {
                "type": "object",
                "properties": {
                    "requirements": {
                        "type": "array", "items": {"type": "string"},
                        "description": "User requirements/constraints to preserve; empty if none.",
                    }
                },
            }
        return result

    return portable(source)


def validate_task_graph(tasks: list[MissionTask]) -> None:
    ids = [task.task_id for task in tasks]
    if len(set(ids)) != len(ids):
        raise ContractError("task IDs must be unique")
    known = set(ids)
    remaining = {}
    for task in tasks:
        deps = set(task.depends_on)
        if len(deps) != len(task.depends_on):
            raise ContractError(f"{task.task_id}: duplicate dependencies")
        if task.task_id in deps:
            raise ContractError(f"{task.task_id}: cannot depend on itself")
        if deps - known:
            raise ContractError(f"{task.task_id}: unknown dependency")
        remaining[task.task_id] = deps
    done = set()
    while remaining:
        ready = {task_id for task_id, deps in remaining.items() if deps <= done}
        if not ready:
            raise ContractError("task dependencies contain a cycle")
        done.update(ready)
        for task_id in ready:
            del remaining[task_id]


def validate_context(request: MissionRequest, fleet: dict[str, Any]) -> None:
    """Check the shape and references understood by the orchestrator world model.

    Additional world/entity/state properties are preserved. This does not prove
    reachability, task feasibility, or semantic alignment with the user's goal.
    """
    robots = fleet.get("robots")
    if not isinstance(robots, list) or not robots:
        raise ContractError("fleet.robots must be a non-empty list")
    robot_ids = set()
    for robot in robots:
        if not isinstance(robot, dict) or not isinstance(robot.get("id"), str):
            raise ContractError("each fleet robot needs a string id")
        if not robot["id"].strip() or robot["id"] in robot_ids:
            raise ContractError("fleet robot IDs must be non-blank and unique")
        if not isinstance(robot.get("actions"), dict) or not robot["actions"]:
            raise ContractError("each fleet robot needs an action list")
        robot_ids.add(robot["id"])

    world = request.world
    entities = world.get("entities")
    states = world.get("robots")
    if not isinstance(entities, dict) or not entities:
        raise ContractError("world.entities must be a non-empty object")
    if not isinstance(states, dict) or not states:
        raise ContractError("world.robots must be a non-empty object")
    kinds = {}
    parents = {}
    for name, entity in entities.items():
        kind = entity if isinstance(entity, str) else (
            entity.get("kind") if isinstance(entity, dict) else None
        )
        if kind not in ("object", "container", "door", "surface", "room"):
            raise ContractError(f"entity {name}: unsupported or missing kind")
        if name in robot_ids:
            raise ContractError("entity IDs must not collide with fleet robot IDs")
        kinds[name] = kind
        parents[name] = entity.get("in") if isinstance(entity, dict) else None
    references = set(entities) | robot_ids
    for name, parent in parents.items():
        if parent is not None and (not isinstance(parent, str) or parent not in references):
            raise ContractError(f"entity {name}: unknown parent")
    for name in parents:
        seen = set()
        current = name
        while current in parents:
            if current in seen:
                raise ContractError("entity containment contains a cycle")
            seen.add(current)
            current = parents[current]
        if kinds[name] == "door":
            connects = entities[name].get("connects") if isinstance(entities[name], dict) else None
            if (not isinstance(connects, list) or len(connects) != 2
                    or any(not isinstance(room, str) or kinds.get(room) != "room" for room in connects)
                    or connects[0] == connects[1]):
                raise ContractError(f"door {name}: connects must name two different rooms")

    opened = world.get("open", [])
    if not isinstance(opened, list) or any(
        not isinstance(name, str) or kinds.get(name) not in ("door", "container")
        for name in opened
    ):
        raise ContractError("world.open must list known doors or containers")
    passages = world.get("passages", [])
    if not isinstance(passages, list) or any(
        not isinstance(pair, list) or len(pair) != 2 or any(
            not isinstance(room, str) or kinds.get(room) != "room" for room in pair
        ) for pair in passages
    ):
        raise ContractError("world.passages must contain pairs of known rooms")
    for robot_id, state in states.items():
        if robot_id not in robot_ids or not isinstance(state, dict):
            raise ContractError("world.robots must contain states for known fleet robots")
        for key in ("at", "holding", "room"):
            value = state.get(key)
            if value is not None and (not isinstance(value, str) or value not in references):
                raise ContractError(f"robot {robot_id}: unknown {key} reference")
        if "airborne" in state and type(state["airborne"]) is not bool:
            raise ContractError(f"robot {robot_id}: airborne must be boolean")


def reject_constant(value):
    raise ContractError("non-finite JSON numbers are not supported")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON object keys are not supported")
        result[key] = value
    return result


def parse_json(text: str) -> Any:
    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)
