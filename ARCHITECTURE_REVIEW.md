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

## Findings fixed

| Finding | Reproduced behavior | Fix and regression coverage |
| --- | --- | --- |
| Inconsistent input validation | Reasoning consumed a generation call when the drone's explicit room disagreed with its location. | Share the interface's world/fleet validation with reasoning preflight and both CLIs. Invalid observations, malformed registry data and non-finite values fail before model calls. |
| Inactive robots visible to reasoning | An extra registered robot absent from the world snapshot was included in the planner prompt. | Filter the fleet supplied to both agents while validating inputs against the complete registry. Preserve the caller's registry unchanged. |
| Lift requirement could be ignored | A search-only task with `payload_kg` passed because an eligible handler existed somewhere in the fleet. | Require a `grab` in an allocated task declaring a lifting weight, and check every grab handler's capacity. Allocation remains the orchestrator's choice. |
| Simulation order hid missing synchronization | Removing the drone crossing's dependency on door opening still returned a valid plan because the simulator happened to open the door first. | Track modeled reads/writes of door/container state, object locations and carrier positions. Reject conflicting accesses without a dependency path, including closing a door before another robot finishes crossing. Independent reads can remain parallel. |
| Carrier location resolution depended on registry order | Initializing a robot located on another robot could access the carrier before its state existed. | Initialize every robot state before resolving rooms; test the dependent robot appearing first in the registry. |

The orchestrator prompt now explicitly covers shared door/container state and the
temporary placement/re-grasp sequence needed to free a gripper before closing a
container. These reminders improve generation; validation remains authoritative.
The offline demo is labelled as fixture replay, since its responses are assembled
from repository examples rather than obtained from live inference.

## Validation evidence

Offline checks: **98 tests passed** across both suites, including an offline
subprocess run of the complete demo without API credentials, invalid-input and
failed-dispatch cases, active fleet filtering, constraints, dependency cycles,
shared-state synchronization, and symbolic goal-state checks.

```bash
uv sync --locked
uv run python -m unittest discover -s reasoning-layer/tests -t reasoning-layer -q
uv run python -m unittest discover -s tests -q
uv run python demo_pipeline.py
```

Live test used the example request and the configured Gemini Developer API model
`gemini-3.5-flash-lite`. The first full run made two generation calls. Its accepted
reasoning candidate was saved; its dispatch was correctly rejected for reopening
an open door and closing the fridge with an occupied gripper. No dispatch file
was published from that rejected candidate. After the prompt clarification, one
explicit cached-handoff run made one orchestration call and passed.

Total live usage in this review: **3 generation calls, 18,471 reported tokens**.
Each call retained the 2,048 output-token and 30,000 prompt-character caps, with no
automatic retries or model fallback. Billing settings were not changed.

Successful result: **3 tasks, 3 allocations, 19 steps, 3 robots**, no dispatch
errors or warnings. A separate invocation of the standalone validator returned
`VALID`. Inspecting the simulated final state confirmed that the apple and juice
bottle reached `coffee_table`, the fridge was closed, all grippers were free,
and the drone was landed. Mission identity and the input world snapshot were
preserved in the published JSON; simulation operates on a separate copy.

Local, ignored evidence files:

- `outputs/integration-review-20261031/candidate.json`
- `outputs/integration-review-20261031/dispatch.json`
- `outputs/integration-review-20261031/verification.json`

To run another live full pipeline, choose new output filenames:

```bash
uv run python reasoning-layer/run_mission.py \
  --input reasoning-layer/examples/request.json \
  --handoff-output outputs/my-candidate.json \
  --output outputs/my-dispatch.json
```

## Remaining architectural boundaries

- The result is dispatch JSON. ROS 2 transport/logging, PX4/Nav2/VLA executors,
  real robot commands and hardware execution have not been implemented or tested.
- The proposed execution feedback arrows are not wired. There is no authoritative
  task lifecycle, execution event stream, plan revision/reconciliation or recovery
  loop yet; these remain outside the scope agreed for this pass.
- Text requirements generate advisory interface review notices. Their meaning and
  alignment with the natural-language goal are not mechanically proven. Dispatch
  simulator warnings, in contrast, prevent publication.
- The simulator models symbolic rooms, doors, objects and grippers. Shared-state
  ordering checks cover those modeled accesses, not geometric collision freedom,
  timing, perception uncertainty or every physical execution possibility. Routing
  uses one available BFS route and may conservatively reject a multi-route plan.
- Task dependencies currently mean every prerequisite step, including cleanup,
  finishes before a dependent task starts. Observation/output references and
  earlier search-result events need a separate agreed contract.

These tests establish integration of the central planning layers. They do not
establish end-to-end execution of the entire proposed robot architecture.
