# System Vision and Architecture

**Physical AI: A Hybrid VLA-Driven Architecture for Multi-Robot Autonomous Missions**
AUC Graduation Project 2026–27 · Supervisor: Prof. Tamer ElBatt · Industry: Dr. Mohamed AbdElSalam, Eng. Mohamed Ellethy (Siemens EDA)

| | |
|---|---|
| Document status | **Draft v0.1 — single source of truth for both repositories, awaiting team + supervisor review** |
| Location | `centralized/docs/`. The `decentralised` repo links here instead of copying it. Moving it to a shared repo is open decision D6. |
| Supersedes | Nothing; reconciles the proposal, literature review, Progress Report 1, and both repositories |
| Owner | Team (one named maintainer per section — TBD) |
| Change rule | Changes to responsibilities, interfaces or model roles require an ADR (`docs/adr/`) |
| Related | [`INTERFACE_CONTRACTS.md`](INTERFACE_CONTRACTS.md) · [`DECISIONS.md`](DECISIONS.md) · [`MODEL_SELECTION.md`](MODEL_SELECTION.md) · [`ROADMAP.md`](ROADMAP.md) · [`AGENT_WORKFLOW.md`](AGENT_WORKFLOW.md) |

Status labels used throughout:

| Label | Meaning |
|---|---|
| ✅ **Tested** | Implemented, with automated tests or a reproducible run that passed |
| 🟡 **Unvalidated** | Implemented, but not validated end-to-end |
| 🧪 **Experimental** | Being investigated; may be discarded |
| 📋 **Planned** | Agreed design, not built |
| ❓ **Undecided** | Proposed; needs a decision (see §14) |

---

## 1. Problem

Foundation models can interpret natural-language missions and reason about scenes, but they do not guarantee real-time, deterministic or safe behaviour. Classical autonomy stacks (PX4, Nav2) are reliable but cannot interpret open-ended goals. Multi-robot coordination adds allocation, sequencing and shared-state problems neither solves alone.

**Research question** (from the proposal): *How can we design a unified robotics architecture that integrates reasoning AI, classical autonomy stacks, and multi-robot orchestration to enable scalable, adaptive and reliable autonomous systems?*

**Gap addressed** (from the literature review §4): VLA research targets single robots; multi-robot systems use LLMs/VLMs only for high-level planning. We study a hybrid design where foundation models act at both mission level and robot level, behind validated, model-independent interfaces.

## 2. Objectives and scope

### Objectives (unchanged from the proposal)

1. A reference hybrid architecture: foundation-model reasoning + classical autonomy + multi-robot orchestration.
2. Build and validate it in NVIDIA Isaac Sim first.
3. Scale from one robot to a heterogeneous team (drone + PuppyPi).
4. Deploy selected components on AUC lab hardware and evaluate sim-to-real.

### Thesis contributions (from the literature review §5.2, retained)

1. A hybrid architecture where the reasoning component can be an LLM, VLM or VLA, enabling comparison within the same system.
2. A structured mission interface that validates AI-generated plans before execution.
3. A closed-loop feedback mechanism where execution results drive replanning.

### In scope

Natural-language missions in indoor, symbolic-map environments (rooms, doors, containers, objects); two robot types (PX4 quadrotor, Hiwonder PuppyPi); Isaac Sim; ROS 2 Jazzy; offline and live evaluation.

### Out of scope

Training a VLA from scratch; outdoor GPS navigation; adversarial security beyond input validation; more than one instance of each robot on hardware (simulation may use more); human–robot dialogue beyond clarification questions.

### Scope question — TurtleBot

The centralized fleet includes `turtlebot_1`, which is not in the proposal. See decision D3.

## 3. Architecture overview

```
┌──────────────────────────── CENTRALIZED (repo: centralized) ───────────────────────────┐
│ Natural-language mission + world snapshot                                              │
│        │                                                                               │
│  ① Reasoning & Mission Planning ── LLM (Gemini) ──▶ candidate tasks / clarify / infeasible │
│        │                                                                               │
│  ② Structured Mission Interface ── deterministic ──▶ validated tasks                   │
│        │                                                                               │
│  ③ Mission Orchestration ── LLM proposes, validate_plan.py disposes ──▶ dispatch plan   │
│        │                         ▲                                                     │
│  ④ Execution Manager  📋  ────────┘ replan requests        ⑤ World Model 📋 (authoritative) │
│        │ step goals / cancels             ▲ results, observations, heartbeats          │
└────────┼─────────────────────────────────┼─────────────────────────────────────────────┘
         │      ROS 2 Jazzy (contracts v0.1, see INTERFACE_CONTRACTS.md)      📋
┌────────▼─────────────────────────────────┴──── DECENTRALIZED (repo: decentralised) ────┐
│ Per robot, namespace /<robot_id>/                                                      │
│  ⑥ Step Executor (deterministic shell): validates goal, enforces safety, timeouts      │
│        │  ⇅  ⑦ Action Selector (VLA slot): VLM 🧪 | rule-based 📋 — closed action menu   │
│        ▼                                                                               │
│  ⑧ Capability layer: Nav2 / gait controller (PuppyPi) · PX4 (drone) · skills           │
│        ▼                                                                               │
│  ⑨ Platform driver: Isaac Sim (✅ gait, no ROS yet) │ real robot (📋)                   │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

The hybrid split: the **center decides what and who** (global view, dependencies, shared state); **each robot decides how, step by step, from what it sees**, within a closed action menu, and classical stacks decide **motion**.

## 4. Layer responsibilities

| # | Layer | Does | Does NOT | Status | Code |
|---|---|---|---|---|---|
| ① | Reasoning & Mission Planning | Decompose mission into tasks; dependencies; constraints; flag `needs_clarification` / `infeasible` | Assign robots; produce actions; talk to robots | ✅ (offline tests); 🟡 live (3 calls) | `centralized/reasoning-layer/reasoning/planner.py` |
| ② | Structured Mission Interface | Schema, task graph, world/fleet consistency, active robots, capability/payload checks; binds mission ID and world | Plan; allocate | ✅ | `centralized/structured_mission_interface/` |
| ③ | Mission Orchestration | Allocate tasks to robots; choose actions from per-robot lists; sequence; cross-robot `depends_on`; symbolic validation; report `unassigned` | Execute; trust its own LLM output | ✅ offline; 🟡 live | `reasoning/orchestration.py`, `mission-orchestrator/` |
| ④ | Execution Manager | Dispatch steps whose deps succeeded; track step lifecycle; timeouts; retries; raise replan requests | Decide allocations itself | 📋 | — |
| ⑤ | World Model | Hold authoritative world + `world_revision`; merge observations | Accept full-world overwrites from robots | 📋 | — |
| ⑥ | Step Executor | Accept/reject step goals; map action → primitives; enforce safety limits, iteration caps, timeouts; report exactly one terminal result | Plan beyond its step; change the world model directly | 📋 (diagrammed) | `decentralised/docs/architecture.dot` |
| ⑦ | Action Selector | Given step + camera image + last outcome, choose the next primitive from a closed menu | Emit motor commands; invent actions | 🧪 SmolVLM weights only | — |
| ⑧ | Capability layer | Navigation (Nav2), gait, PX4 flight modes, manipulation skills | Interpret language | 📋 | — |
| ⑨ | Platform driver | Joint targets / `/cmd_vel` → robot; sensors → ROS topics | — | ✅ scripted gait in sim (no ROS) | `decentralised/hello_puppy.py` |

## 5. Centralized vs. decentralized intelligence

| Decision | Where | Why |
|---|---|---|
| Mission decomposition | Center (LLM) | Needs global goal and full fleet |
| Allocation and cross-robot ordering | Center (LLM + validator) | Shared state (doors, objects) needs one arbiter — literature review §3.2.2 notes decentralized-only systems suffer state inconsistency |
| Next primitive within a step | Robot (selector) | Needs fresh local perception; tolerates network delay |
| Motion, balance, flight control, obstacle stop | Robot (classical) | Real-time and safety-critical; never model- or network-dependent |
| Replanning | Center, triggered by robot results | Event-driven replanning (CoMuRoS / COHERENT pattern in the review) |

## 6. Model responsibilities and rationale

Full evidence: `MODEL_SELECTION.md`.

**Design position:** a VLA is a per-robot model — it maps one robot's camera view and an instruction to that robot's actions — so it belongs on the decentralized agents, not in the center. The center plans across robots, which is a language-reasoning task, so it uses an LLM. Today no available VLA both runs on our edge hardware and supports a quadruped or a quadrotor, so each agent uses a **VLM in the VLA's slot**: it sees the camera image and picks the next action from the robot's closed action list, and a deterministic executor turns that into motion. The agent interface is designed so a VLA can replace the VLM when a suitable one exists.

| Role | Current | Rationale | Status |
|---|---|---|---|
| Mission reasoning, orchestration (center) | LLM: Gemini (`gemini-3.5-flash-lite`) via the `PlanProvider` interface | Mission decomposition and multi-robot allocation are language/reasoning tasks; strong structured output; swappable provider | ✅/🟡 |
| Robot action selection (agent — the VLA slot) | Small VLM (SmolVLM family) choosing from a closed per-robot action list with JSON-constrained output; rule-based selector as baseline | No edge-deployable VLA supports our robots: OpenVLA (7B) needs ~15 GB GPU memory, runs ~6 Hz on an RTX 4090 and was trained only on single-arm manipulators; quadruped/drone VLAs (NaVILA, QUAR-VLA, CognitiveDrone) are research models, and NaVILA itself is hierarchical (VLA → mid-level commands → locomotion policy). See [`MODEL_SELECTION.md`](MODEL_SELECTION.md). | 🧪 |
| Manipulation (agent; only if PuppyPi has an arm) | Candidate VLA: SmolVLA (450M) fine-tuned with LeRobot, wrapped as a skill | Embodiment matches VLA training data; small enough for consumer GPU | ❓ D4 |
| Locomotion / flight (agent) | Scripted gait → Nav2; PX4 | Classical stacks required by the proposal | ✅ gait in sim / 📋 |

**Deviation from the proposal, to be confirmed (D1):** the proposal lists OpenVLA "for high-level reasoning, orchestration and mission planning", i.e. in the center. We place the VLA role on the agents instead, where a VLA's per-robot perception-to-action mapping applies, and fill that role with a VLM until an edge-deployable VLA for our platforms exists. The proposal's objective — foundation-model reasoning combined with classical autonomy across a multi-robot team — is unchanged.

## 7. Communication and interfaces

ROS 2 Jazzy (matches Ubuntu 24.04 and Isaac Sim 6.1's default bridge). Summary — full contract in `INTERFACE_CONTRACTS.md`:

| Interface | Mechanism | Direction |
|---|---|---|
| Step assignment | Action `/<robot_id>/execute_step` | center → robot |
| Step result + observations | Action result | robot → center |
| Heartbeat / status | Topic `/<robot_id>/status` 2 Hz | robot → center |
| Observations outside steps | Topic `/fleet/observations` | robot → center |
| Current plan (monitoring) | Topic `/mission/plan`, transient-local | center → all |
| Replan requests | Topic `/mission/replan_requests` | center internal + logging |
| E-stop | Topic `/fleet/estop` | any → robots |

The centralized pipeline stays runnable without ROS 2 (offline tests, JSON in/out). ROS 2 is added as a thin node around it.

## 8. World state

Authoritative world: centralized World Model (schema = today's `world` object). Robots send typed observations with confidence and provenance, never full worlds. Plans carry `world_revision`. Details: contracts §6.

## 9. Robot-specific execution

### PuppyPi (Hiwonder; exact kit TBD — D4)

- Sim: Isaac Sim 6.1, URDF-derived USD, 8 DOF (hip + knee per leg), PhysX. Scripted trot gait verified: 0.45 m forward in 3 s, ~110° turn in 3 s.
- Target stack: `/cmd_vel` → gait driver (📋) → Nav2 + localization in a known map (📋) → executor with `movetowards`, `search`, `inspect`, `push` (📋). `open/close/grab/put*` only if an arm is confirmed.
- Real robot: Hiwonder ships a Raspberry Pi 5 board and ROS support; integration plan after sim milestones.

### Drone (PX4; airframe and companion computer TBD — D5)

- Target: PX4 SITL with ROS 2 via uXRCE-DDS; actions `takeoff_from`, `land_on`, `movetowards`, `search`, `inspect`.
- Simulator choice is open: the Pegasus Simulator (Isaac Sim + PX4) lists support up to Isaac Sim 5.1, while we use 6.1. Options in D7.

## 10. Simulation and deployment strategy

1. Everything first in Isaac Sim 6.1 (pinned), headless-capable for CI-like runs on the workstation.
2. The same ROS 2 topics/actions in sim and on hardware; only the platform driver changes (`use_sim_time`).
3. Model inference on the workstation over ROS 2 for sim and first hardware tests; onboard inference only if measured latency allows (D2).
4. Hardware: PuppyPi first (lower risk), then drone with a safety pilot and geofence.

## 11. Safety boundaries and failure handling

| Boundary | Rule |
|---|---|
| Model output → execution | Never direct. Every LLM/VLM output passes schema + semantic validation (existing for ①–③; required for ⑦). |
| Action menu | Executors accept only actions in their `robots.json` entry; selectors choose only from the closed primitive menu. |
| Real-time control | Balance, PX4 attitude/position control, obstacle stop and e-stop never depend on a model or the network. |
| Timeouts | Every step has `timeout_s`; every selector loop has a max-iteration cap. |
| Comms loss | Robot: stop and hold (PuppyPi) / PX4 failsafe (drone) after link-loss timeout. Center: step `FAILED/COMMS_LOST`, replan. |
| Retries | At most one automatic retry per step, then replan. No unbounded loops (matches `SKILL.md`). |
| Hardware | Drone: geofence, safety pilot with RC override, PX4 failsafes. PuppyPi: tilt/fall detection → sit and stop. |

## 12. Evaluation criteria

| Metric | Definition | Where measured |
|---|---|---|
| Plan validity rate | % of live LLM plans passing ②+③ validation on first attempt | Mission benchmark set (📋 ≥ 20 missions) |
| Mission success rate | % of missions reaching the goal world state | Sim, then hardware |
| Step success rate | % of steps ending `SUCCEEDED`, per robot and action | Execution logs |
| Selector accuracy | % of selector decisions that are valid **and** correct vs. scripted ground truth | Sim episodes |
| Selector format validity | % of outputs that parse into an allowed primitive | Benchmark |
| Recovery rate | % of injected failures (object moved, door closed, robot dropout) recovered via replanning | Fault-injection scenarios |
| Latency | Mission → first dispatch; per-step decision latency; replan latency | Logs |
| Cost | LLM tokens and calls per mission | Provider usage logs (already recorded) |
| Sim-to-real gap | Step success sim vs. hardware for the same scenarios | Milestone 4 |
| Baselines | Rule-based selector vs. VLM; LLM orchestrator vs. classical allocator (optional) | All above |

Targets (proposed, to be agreed): plan validity ≥ 90%, sim mission success ≥ 80% on the benchmark set, recovery ≥ 70% of injected faults, decision latency ≤ 2 s.

## 13. Current implementation status (2026-11-08)

| Component | Status |
|---|---|
| Reasoning layer | ✅ 62 offline tests; 🟡 live Gemini runs (3 calls, 2026-10-31) |
| Structured Mission Interface | ✅ 36 tests |
| Orchestrator + `validate_plan.py` | ✅ offline demo; 🟡 live |
| Execution Manager, World Model, replanning | 📋 (replanning described in `SKILL.md` only) |
| Interface contracts | 📋 draft v0.1 |
| ROS 2 (both repos) | 📋 nothing implemented |
| PuppyPi in Isaac Sim (load, physics, gait) | ✅ verified headless (manual check; no automated test yet) |
| PuppyPi camera, `/cmd_vel`, Nav2 | 📋 |
| Step executor, semantic map | 📋 (diagram) |
| SmolVLM action selection | 🧪 weights downloaded; inference not yet run from the repo environment |
| Drone / PX4 | 📋 nothing implemented |
| Hardware | 📋 none |

## 14. Unresolved decisions

Full list with options: `DECISIONS.md`. Headlines: D1 VLA role moved from the center to the agents, filled by a VLM for now; D2 inference location; D3 TurtleBot in scope?; D4 PuppyPi kit and arm; D5 drone hardware; D6 where shared contracts live; D7 drone simulator; D8 Isaac Sim install method.

## 15. Milestones

Mapped to the proposal's four milestones. Detail: `ROADMAP.md`.

| Proposal milestone | Engineering phases |
|---|---|
| M1 Literature review & architecture | P0 Architecture consolidation (this document) |
| M2 Single cyber-physical system | P1 Reproducible PuppyPi sim · P2 Executor loop · P3 Dispatch → PuppyPi · P4 ROS 2 feedback · P5 VLM selection |
| M3 Multi-robot simulation | P6 Drone + PX4 · P7 Coordinated missions + replanning |
| M4 Real-world deployment | P8 Robustness, hardware, sim-to-real evaluation |
