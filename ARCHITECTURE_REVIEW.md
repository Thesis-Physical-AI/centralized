# Central layer integration review

Reviewed branch: `feat/structured-interface-integration`, starting at
`9a78afb506de7302a7a60b0f5741e5e81f09b140`, on 31 October 2026.
Reference: [the proposed architecture](assets/Thesis%20Project%20Proposal.png).
Scope agreed for this pass: review, fix and test the existing three central
layers. Execution feedback/replanning, ROS 2 and robot executors are future work.

## Architecture alignment

The central planning path follows the proposed responsibilities:

| Proposed layer | Current implementation | Responsibility |
| --- | --- | --- |
| Reasoning and Mission Planner | `reasoning-layer/reasoning/planner.py` | Gemini proposes high-level tasks, dependencies and constraints, or requests clarification/reports infeasibility. It does not allocate robots or generate action steps. |
| Structured Mission Interface | `structured_mission_interface/`, bridged by `reasoning-layer/reasoning/interface.py` | Checks schema, world/fleet consistency, task graph and explicit capability/payload constraints. Binds accepted tasks to the original mission ID and world snapshot. |
| Mission Orchestrator | `reasoning-layer/reasoning/orchestration.py`, using `mission-orchestrator/SKILL.md` | Gemini allocates active robots and proposes action steps. The host checks constraints, renders instructions and adds declared task dependency barriers. |
| Dispatch validation | `mission-orchestrator/validate_plan.py` | Checks action arguments, modeled physical preconditions and ordering of shared-state accesses before publishing JSON. This is a host gate after orchestration. |

The shared interface also supplies world/fleet preflight checks before reasoning.
This avoids spending quota on invalid observations; it does not move task planning
into the interface. Registry presence alone does not mean availability: only
robots represented in the supplied `world.robots` snapshot are treated as active.
The caller remains responsible for providing current observations.