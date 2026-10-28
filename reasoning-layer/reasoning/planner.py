"""Provider-independent planning, bounded format repair, and trusted handoff."""

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from .contracts import ContractError, MissionRequest, ModelDecision, generation_schema, parse_json, validate_context

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


def build_prompt(request: MissionRequest, fleet: dict[str, Any]) -> str:
    return json.dumps({
        "mission_id": request.mission_id,
        "natural_language": request.natural_language,
        "world": request.world,
        "fleet": fleet,
    }, ensure_ascii=False, allow_nan=False)


class MissionPlanner:
    def __init__(self, provider: PlanProvider, *, max_attempts: int = 1):
        if max_attempts not in (1, 2):
            raise ValueError("max_attempts must be 1 or 2")
        self.provider = provider
        self.max_attempts = max_attempts

    def plan(self, request: MissionRequest, fleet: dict[str, Any]) -> PlanningOutcome:
        validate_context(request, fleet)
        prompt = build_prompt(request, fleet)
        schema = generation_schema()
        for attempt in range(1, self.max_attempts + 1):
            response = self.provider.generate(system=SYSTEM_PROMPT, prompt=prompt, schema=schema)
            try:
                if not isinstance(response, str) or len(response) > 100000:
                    raise ContractError("model response must be text under 100000 characters")
                decision = ModelDecision.model_validate(parse_json(response))
                # Reject non-JSON values inside free-form constraints as well.
                json.dumps(decision.model_dump(), allow_nan=False)
            except (ValidationError, ValueError, TypeError) as error:
                if attempt == self.max_attempts:
                    raise GenerationError("Model output failed the task contract after bounded repair.") from None
                if isinstance(error, ValidationError):
                    details = [{"field": list(item["loc"]), "error": item["msg"]}
                               for item in error.errors(include_input=False, include_url=False)]
                else:
                    details = [{"error": "Invalid JSON, task IDs, or task dependencies."}]
                # A fresh request carries the original context and validation feedback.
                # Do not treat model output as system instructions or execute it.
                prompt = json.dumps({
                    "original_request": json.loads(build_prompt(request, fleet)),
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
