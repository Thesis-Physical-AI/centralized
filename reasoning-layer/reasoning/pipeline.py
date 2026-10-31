"""Reasoning -> interface -> orchestrator -> dispatch validation."""

from dataclasses import dataclass, field
from copy import deepcopy
from typing import Callable

from .contracts import MissionRequest
from .interface import validate_handoff
from .orchestration import MissionOrchestrator
from .planner import MissionPlanner


@dataclass
class PipelineOutcome:
    status: str
    dispatch: dict | None = None
    report: dict = field(default_factory=dict)
    interface_warnings: list[dict] = field(default_factory=list)


class MissionPipeline:
    def __init__(self, planner: MissionPlanner, orchestrator: MissionOrchestrator):
        self.planner = planner
        self.orchestrator = orchestrator

    def run(self, request: MissionRequest, fleet: dict, *, handoff: dict | None = None,
            on_handoff: Callable[[dict], None] | None = None) -> PipelineOutcome:
        if handoff is None:
            result = self.planner.plan(request, fleet)
            if result.handoff is None:
                return PipelineOutcome(result.decision.status, report={
                    "mission_id": request.mission_id,
                    **result.decision.model_dump(exclude={"tasks"}),
                })
            handoff = result.handoff
        interface_warnings = []
        accepted = validate_handoff(handoff, request, fleet, warnings=interface_warnings)
        if on_handoff is not None:
            on_handoff(deepcopy(accepted))
        active_fleet = deepcopy(fleet)
        active_fleet["robots"] = [robot for robot in active_fleet["robots"]
                                  if robot["id"] in accepted["world"]["robots"]]
        result = self.orchestrator.orchestrate(accepted, active_fleet)
        if result.plan is not None:
            return PipelineOutcome("ready", dispatch=result.plan, interface_warnings=interface_warnings)
        return PipelineOutcome(result.status, report={
            "mission_id": request.mission_id,
            "status": result.status,
            "errors": result.errors,
            "warnings": result.warnings,
            "unassigned": result.unassigned,
        }, interface_warnings=interface_warnings)
