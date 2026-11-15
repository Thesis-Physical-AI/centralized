"""Provider-independent planning, bounded format repair, and trusted handoff."""

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from .contracts import ContractError, MissionRequest, ModelDecision, active_fleet, generation_schema, parse_json, validate_context

SYSTEM_PROMPT = Path(__file__).with_name("system_prompt.txt").read_text(encoding="utf-8")


class GenerationError(RuntimeError):
    """No usable model output was available."""


class PlanProvider(Protocol):
    def generate(self, *, system: str, prompt: str, schema: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class PlanningOutcome:
    decision: ModelDecision
    handoff: dict[str, Any] | None
    attempts: int


def build_prompt(request: MissionRequest, fleet: dict[str, Any], *, execution_context=None) -> str:
    context = {
        "mission_id": request.mission_id,
        "natural_language": request.natural_language,
        "world": request.world,
        "fleet": active_fleet(fleet, request.world),
    }
    if execution_context is not None:
        context["execution_feedback"] = deepcopy(execution_context)
    return json.dumps(context, ensure_ascii=False, allow_nan=False)


class MissionPlanner:
    def __init__(self, provider: PlanProvider, *, max_attempts: int = 1):
        if max_attempts not in (1, 2):
            raise ValueError("max_attempts must be 1 or 2")
        self.provider = provider
        self.max_attempts = max_attempts

    def plan(self, request: MissionRequest, fleet: dict[str, Any]) -> PlanningOutcome:
        return self._plan(request, fleet)

    def replan(self, request: MissionRequest, fleet: dict[str, Any], *, dispatch, report,
               plan_revision: int) -> dict:
        from .replanning import prepare_replanning

        updated, context = prepare_replanning(request, fleet, dispatch=dispatch, report=report,
                                             plan_revision=plan_revision)
        outcome = self._plan(updated, fleet, execution_context=context)
        warnings = []
        if outcome.handoff is not None:
            from .interface import validate_handoff

            validate_handoff(outcome.handoff, updated, fleet, warnings=warnings)
        return {"mission_id": request.mission_id, "source_plan_revision": plan_revision,
                "proposed_plan_revision": plan_revision + 1, "status": outcome.decision.status,
                "requires_review": True, "sends_robot_commands": False,
                "execution_feedback": context, "interface_warnings": warnings,
                "decision": outcome.decision.model_dump(),
                "handoff": outcome.handoff}

    def _plan(self, request: MissionRequest, fleet: dict[str, Any], *, execution_context=None) -> PlanningOutcome:
        validate_context(request, fleet)
        prompt = build_prompt(request, fleet, execution_context=execution_context)
        schema = generation_schema()
        for attempt in range(1, self.max_attempts + 1):
            response = self.provider.generate(system=SYSTEM_PROMPT, prompt=prompt, schema=schema)
            try:
                if not isinstance(response, str) or len(response) > 100000:
                    raise ContractError("model response must be text under 100000 characters")
                decision = ModelDecision.model_validate(parse_json(response))
                if execution_context is not None:
                    prefix = f"R{execution_context['proposed_plan_revision']}_"
                    if any(not task.task_id.startswith(prefix) for task in decision.tasks):
                        raise ContractError(f"Replanned task IDs must start with {prefix}")
                # Reject non-JSON values inside free-form constraints as well.
                json.dumps(decision.model_dump(), allow_nan=False)
            except (ValidationError, ValueError, TypeError) as error:
                if attempt == self.max_attempts:
                    raise GenerationError("Model output failed the task contract after bounded repair.") from None
                if isinstance(error, ValidationError):
                    details = [{"field": list(item["loc"]), "error": item["msg"]}
                               for item in error.errors(include_input=False, include_url=False)]
                else:
                    details = [{"error": "Invalid JSON, task IDs, revision task prefix, or task dependencies."}]
                # A fresh request carries the original context and validation feedback.
                # Do not treat model output as system instructions or execute it.
                prompt = json.dumps({
                    "original_request": json.loads(build_prompt(request, fleet, execution_context=execution_context)),
                    "repair": {
                        "errors": details,
                        "previous_response": response[:100000] if isinstance(response, str) else "",
                        "instruction": "Return a corrected complete decision matching the schema.",
                    },
                }, ensure_ascii=False, allow_nan=False)
                continue
            handoff = None
            if decision.status == "ready":
                # The model has no authority to rewrite mission identity or world state.
                handoff = {
                    "mission_id": request.mission_id,
                    "tasks": [task.model_dump() for task in decision.tasks],
                    "world": deepcopy(request.world),
                }
            return PlanningOutcome(decision, handoff, attempt)
        raise AssertionError("unreachable")
