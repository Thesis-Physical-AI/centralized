"""Replay repository fixture responses through the real pipeline without API calls."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "reasoning-layer"))

from reasoning.contracts import MissionRequest, parse_json
from reasoning.orchestration import MissionOrchestrator
from reasoning.pipeline import MissionPipeline
from reasoning.planner import MissionPlanner


class FixtureDemoProvider:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        return json.dumps(next(self.responses))


def main():
    def read(relative):
        return parse_json((ROOT / relative).read_text(encoding="utf-8"))

    request = MissionRequest.model_validate(read("reasoning-layer/examples/request.json"))
    fleet = read("mission-orchestrator/robots.json")
    handoff = read("reasoning-layer/examples/expected_handoff.json")
    reference = read("mission-orchestrator/examples/dispatch_plan.json")
    decision = {"status": "ready", "tasks": handoff["tasks"], "questions": [], "reasons": []}
    draft = {
        "allocations": reference["allocations"],
        "steps": [{key: value for key, value in step.items() if key != "instruction"}
                  for step in reference["steps"]],
        "unassigned": reference["unassigned"],
    }
    provider = FixtureDemoProvider(decision, draft)
    result = MissionPipeline(MissionPlanner(provider), MissionOrchestrator(provider)).run(request, fleet)
    print(json.dumps({
        "mode": "offline_demo_with_fixture_responses",
        "status": result.status,
        "mission_id": request.mission_id,
        "layers": ["reasoning", "structured_mission_interface", "orchestrator", "dispatch_validation"],
        "fixture_responses_consumed": provider.calls,
        "api_calls": 0,
        "sends_robot_commands": False,
        "interface_warnings": result.interface_warnings,
        "step_count": len(result.dispatch["steps"]) if result.dispatch else 0,
        "allocations": result.dispatch["allocations"] if result.dispatch else [],
        "report": result.report,
    }, indent=2))
    return 0 if result.status == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
