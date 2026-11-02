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
                    },
                    "allowed_robots": {"type": "array", "items": {"type": "string"},
                                       "description": "Only explicit user robot restrictions; otherwise omit."},
                    "required_actions": {"type": "array", "items": {"type": "string"},
                                         "description": "Required capability names from the supplied fleet; otherwise omit."},
                    "payload_kg": {"type": "number", "minimum": 0,
                                   "description": "Known per-object lifting requirement; otherwise omit."},
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
    """Use the shared interface's world/fleet checks before spending API quota."""
    from structured_mission_interface import validate_world_context

    result = validate_world_context(request.world, fleet)
    if not result.valid:
        raise ContractError("Invalid mission context: " + "; ".join(
            f"{issue.path}: {issue.message}" for issue in result.errors))


def active_fleet(fleet: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    """Return a detached registry containing only observed, active robots."""
    from copy import deepcopy

    active = deepcopy(fleet)
    active["robots"] = [robot for robot in active["robots"] if robot["id"] in world["robots"]]
    return active


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
