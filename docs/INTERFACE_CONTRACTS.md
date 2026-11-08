# Interface Contracts: Centralized ↔ Decentralized

Contract version: **0.1.0 (draft, proposed)**. Nothing in this document is implemented yet. It extends the existing dispatch plan produced by `centralized` without changing its current fields, so the 98 existing tests remain valid.

## 1. Design rules

1. **Model-independent.** Messages carry actions, arguments, states and observations — never prompts, tokens, or model-specific fields. Gemini, a local LLM, SmolVLM, a VLA or a rule-based selector can be swapped without changing a message.
2. **One source.** JSON Schemas in `contracts/schemas/*.schema.json` are canonical. ROS 2 `.msg`/`.action` definitions mirror them. Python models in both repos are generated from or tested against the schemas.
3. **Versioned.** Every message carries `contract_version` (semver). A receiver rejects a major version it does not support with reason `UNSUPPORTED_VERSION`.
4. **Robots report facts, the center owns the world.** No robot sends a full `world` object.
5. **Every state change is explicit and acknowledged.** No silent drops: a step ends in exactly one terminal state.

## 2. Identifiers

| ID | Format | Owner | Notes |
|---|---|---|---|
| `mission_id` | string, as today (`M-001`) | Caller of the pipeline | Already enforced by the centralized app (not the LLM) |
| `plan_revision` | integer ≥ 0 | Orchestrator | 0 = first dispatch; +1 on every replan. Executors reject steps from an older revision with `STALE_PLAN`. |
| `task_id` | string (`T1`) | Reasoning layer | Unchanged |
| `step_id` | string, unique within a mission across all revisions (`S12`, `S5b`) | Orchestrator | Field is `id` in today's dispatch plan; contract calls it `step_id`. Adapter maps `id → step_id` (see §8). |
| `robot_id` | string matching `robots.json` (`puppypi_1`) | Fleet registry | Also the ROS 2 namespace: `/puppypi_1/...` |
| `world_revision` | integer | World model (center) | Increments on every accepted observation |
| `event_id` | UUID v4 | Sender | De-duplication of retransmitted messages |

All timestamps: ROS time (`builtin_interfaces/Time`); in sim, `use_sim_time:=true`.

## 3. Step lifecycle (the core contract)

```
            dispatch                 accept
PENDING ─────────────▶ DISPATCHED ─────────▶ EXECUTING ──┬──▶ SUCCEEDED
   │                       │                             ├──▶ FAILED
   │ deps not met          │ reject                      ├──▶ BLOCKED
   ▼                       ▼                             ├──▶ TIMED_OUT
 (waits)               REJECTED                          └──▶ CANCELED
```

- **PENDING / DISPATCHED** are tracked by the orchestrator. A step is dispatched only when every `depends_on` step is `SUCCEEDED`.
- **REJECTED**: executor refused before starting (unknown action, bad args, stale revision, robot busy, e-stopped). No side effects.
- **FAILED**: tried and could not complete (e.g. grasp missed). World may have changed; observations attached.
- **BLOCKED**: precondition not met in reality (door closed, object absent). Typically triggers replanning rather than retry.
- **TIMED_OUT**: exceeded `timeout_s`; executor must have stopped the robot.
- **CANCELED**: center canceled the goal (replan, abort, e-stop).

Terminal states are final. A retry is a new step dispatch with the same `step_id` and `attempt` + 1.

## 4. Messages

### 4.1 Mission dispatch (center → all), informational

Topic `/mission/plan` (`std_msgs`-style JSON envelope or typed msg), QoS reliable + transient-local so late-joining robots see the current plan.

```json
{
  "contract_version": "0.1.0",
  "mission_id": "M-001",
  "plan_revision": 0,
  "world_revision": 0,
  "allocations": [{"task_id": "T1", "robots": ["drone_1"], "rationale": "..."}],
  "steps": [ /* StepAssignment bodies, see 4.2 */ ],
  "unassigned": []
}
```

This is today's dispatch plan plus `contract_version`, `plan_revision`, `world_revision`. Robots do **not** self-start steps from it; it exists for monitoring and logging.

### 4.2 Step assignment (center → one robot)

ROS 2 action `/<robot_id>/execute_step`, type `physical_ai_interfaces/action/ExecuteStep`. Actions (not topics) because steps are long-running, need feedback, and must be cancellable.

**Goal**

| Field | Type | Example |
|---|---|---|
| `contract_version` | string | `0.1.0` |
| `mission_id` | string | `M-001` |
| `plan_revision` | int32 | `0` |
| `step_id` | string | `S12` |
| `task_id` | string | `T3` |
| `attempt` | int32 | `1` |
| `action` | string, must be in this robot's `robots.json` action list | `movetowards` |
| `args` (JSON form) / `args_json` (ROS 2 form, a JSON string) | object of string → entity id, validated against the action's arg kinds in `robots.json` | `{"target": "fridge"}` |
| `depends_on` | string[] — informational; the center only dispatches once these succeeded | `["S2"]` |
| `robot_id` | string | `puppypi_1` |
| `instruction` | string, rendered action syntax (unchanged from today) | `[movetowards] fridge` |
| `timeout_s` | float32 | `60.0` |

**Feedback** (≥ 1 Hz while executing): `state` (`EXECUTING`), `progress` (0–1, −1 if unknown), `detail` (short text), `stamp`.

**Result**

| Field | Type | Notes |
|---|---|---|
| `step_id`, `mission_id`, `plan_revision`, `attempt` | | echo of goal |
| `status` | enum: `SUCCEEDED, FAILED, BLOCKED, TIMED_OUT, CANCELED, REJECTED` | |
| `reason_code` | enum, §5 | empty on success |
| `reason` | string | human-readable |
| `observations` | `Observation[]` | facts learned during the step |
| `started_at`, `ended_at` | Time | |
| `decision_trace_ref` | string | optional pointer to a log record (which model/selector chose what); not interpreted by the center |

### 4.3 Robot status (robot → center), heartbeat

Topic `/<robot_id>/status`, 2 Hz, best-effort.

```json
{"robot_id": "puppypi_1", "state": "IDLE|EXECUTING|ESTOPPED|FAULT|OFFLINE",
 "current_step_id": "S12", "pose": {"frame": "map", "x": 1.2, "y": 0.4, "yaw": 1.57},
 "holding": null, "airborne": false, "battery_pct": 81, "stamp": "..."}
```

The orchestrator marks a robot `OFFLINE` after 3 missed heartbeats (1.5 s) and treats its running step as `FAILED / COMMS_LOST`.

### 4.4 Observation (robot → center)

Embedded in step results, and also streamed on `/fleet/observations` for things noticed outside a step.

```json
{"event_id": "uuid", "robot_id": "drone_1", "mission_id": "M-001", "step_id": "S5",
 "kind": "entity_located | entity_absent | entity_state | robot_state",
 "subject": "apple", "value": {"in": "counter"}, "confidence": 0.82,
 "source": "executor | perception | vlm", "stamp": "..."}
```

`kind` values map onto the existing world model: `entity_located` → updates `entities[subject].in`; `entity_state` → `open` list (`{"open": true}`); `entity_absent` → marks the last known location as wrong; `robot_state` → `robots[robot_id]`.

### 4.5 Replanning request (internal to center; also published for logging)

Topic `/mission/replan_requests`.

```json
{"mission_id": "M-001", "plan_revision": 0, "world_revision": 7,
 "trigger_step_id": "S5", "trigger_status": "BLOCKED", "reason_code": "OBJECT_NOT_FOUND",
 "completed_step_ids": ["S1", "S2", "S3", "S4"],
 "scope": "orchestrator | mission"}
```

`scope: orchestrator` = re-allocate/re-sequence remaining steps (as `SKILL.md` describes). `scope: mission` = goal unreachable with current tasks; reasoning layer must re-decompose.

### 4.6 Emergency stop

Topic `/fleet/estop` (`std_msgs/Bool`-like with `robot_id` or `"*"`), reliable. Every executor must stop motion within its platform's stop time independently of any model. Clearing e-stop never resumes a step automatically.

## 5. Reason codes

| Code | Status | Meaning | Default center response |
|---|---|---|---|
| `UNKNOWN_ACTION` | REJECTED | action not in robot's list | bug: abort mission |
| `INVALID_ARGS` | REJECTED | args fail schema or unknown entity | bug: abort mission |
| `STALE_PLAN` | REJECTED | older `plan_revision` | ignore |
| `ROBOT_BUSY` | REJECTED | another step running | re-dispatch later |
| `ESTOPPED` | REJECTED/CANCELED | e-stop active | hold mission |
| `PRECONDITION_FAILED` | BLOCKED | e.g. door closed, gripper full | replan (orchestrator) |
| `OBJECT_NOT_FOUND` | BLOCKED | search/goto target not observed | replan: search elsewhere |
| `PATH_BLOCKED` | BLOCKED | navigation could not find a path | replan |
| `EXECUTION_ERROR` | FAILED | skill tried and failed | retry once, then replan |
| `SAFETY_LIMIT` | FAILED | fall detected, tilt, geofence, battery | stop robot, replan without it |
| `DECISION_LIMIT` | FAILED | action-selector loop exceeded max iterations | retry once, then replan |
| `COMMS_LOST` | FAILED | (set by center) heartbeat lost | replan without robot |
| `TIMEOUT` | TIMED_OUT | exceeded `timeout_s` | retry once, then replan |

## 6. World-state ownership and synchronization

- **Authoritative world**: one World Model in the centralized system, with `world_revision`. It is the same structure the Structured Mission Interface already validates (`entities`, `open`, `passages`, `robots`).
- **Robots own** only their own `robots[robot_id]` fields (pose/at, holding, airborne) and report them via status/observations.
- **Merge rule**: observations are applied in arrival order; an observation with lower `confidence` than a threshold (configurable, default 0.5) is logged but not applied; conflicting observations about the same subject within one revision trigger a `scope: orchestrator` replan with a `search`/`inspect` step to resolve it.
- **Plans reference a world revision**. A replan always starts from the latest revision and marks completed steps as done (matches `SKILL.md` §Replanning).
- **Local world on robot**: an executor may keep a cache (semantic map: entity → pose) for navigation. It is not authoritative and is refreshed from the center at mission start.

## 7. Action vocabulary mapping (PuppyPi)

Centralized actions stay as defined in `robots.json`. Each executor maps them to its own primitives. For PuppyPi, using the decentralised design:

| Centralized action | Executor path | Primitive(s) the selector may use | Needs |
|---|---|---|---|
| `movetowards{target}` | Nav2 `NavigateToPose` to semantic-map pose | `goto`, `look_around`, `report` | `/cmd_vel` driver, map, localization |
| `search{object, area}` | go to area, rotate, query perception/VLM | `goto`, `look_around`, `report` | camera |
| `inspect{target}` | go to target, capture, describe | `goto`, `look_around`, `report` | camera |
| `push{object}` | scripted push gait | `skill(push)` | — |
| `open/close{target}` | skill | `skill(open/close)` | **arm (hardware TBD)** |
| `grab/putinto/puton` | skill | `skill(...)` | **arm (hardware TBD)** |

Drone: `takeoff_from`, `land_on`, `movetowards` → PX4 offboard/mission commands via ROS 2 (`px4_msgs` through the uXRCE-DDS bridge); `search`/`inspect` → camera + selector. Detailed in a later drone ADR.

## 8. Compatibility with today's centralized output

No breaking change: a thin adapter (`dispatch_adapter.py`, proposed) converts the existing dispatch JSON to contract messages: adds `contract_version`, `plan_revision=0`, `world_revision=0`, renames `id → step_id`, sets `attempt=1` and a default `timeout_s` per action from a config table. Existing `validate_plan.py` and tests are untouched.

## 9. Proposed file layout for the contract

```
contracts/
  VERSION                         0.1.0
  schemas/                        canonical JSON Schemas
    step_assignment.schema.json
    step_result.schema.json
    observation.schema.json
    robot_status.schema.json
    replan_request.schema.json
    dispatch_plan.schema.json
  ros2/physical_ai_interfaces/    ament_cmake package mirroring the schemas
    action/ExecuteStep.action
    msg/Observation.msg  msg/RobotStatus.msg  msg/ReplanRequest.msg  msg/MissionPlan.msg
  examples/                       one valid + one invalid example per schema (used by CI)
```

Where `contracts/` lives is decision D6.
