# Isaac Sim Readiness, Implementation Roadmap, and Risks

The order follows the team's proposed sequence, with two adjustments suggested by the 2026-11-08 repository audit:

1. **Contracts before ROS 2 code** (P0): the two repos already disagree on field names (`id` vs `step_id`) and on world ownership. Fixing that on paper is cheap; fixing it after both sides have ROS nodes is not.
2. **Rule-based action selection before the VLM** (P2): the executor loop needs a deterministic selector anyway (as a baseline for the thesis and a fallback). The VLM then plugs into a loop that already works, so its effect can be measured on its own.

Durations assume ~3 people part-time and are rough.

## P0 — Architecture consolidation (1 week)

| Deliverable | Acceptance |
|---|---|
| `SYSTEM_ARCHITECTURE.md` reviewed by team and supervisor | D1, D3 answered by supervisor |
| ADRs 0001–0007 | Accepted or explicitly left open |
| Contract v0.1: JSON Schemas + examples | Schemas validate today's `dispatch_plan.json` through the adapter |
| `AGENTS.md`, `CLAUDE.md`, PR/issue templates, centralized CI | CI green on `main` |
| Hardware inventory (D4, D5) | Written table from the lab |

## P1 — Reproducible PuppyPi simulation (1–2 weeks)

| Deliverable | Acceptance |
|---|---|
| `docs/DEVELOPMENT.md`: prerequisites (Ubuntu 24.04, driver, GPU), three Python environments, Isaac Sim install (pip 6.1.0.0 or source, D8) | A teammate on a clean machine reaches a passing smoke test from the doc alone |
| `ISAACSIM_PYTHON` discovery; no hard-coded paths | `grep -r "~/Projects" .` empty |
| `install.sh` split: `--sim` (default, no downloads) / `--vlm` (opt-in weights) | Basic sim needs no model download |
| Pinned `requirements-vlm.txt` (or `uv.lock`) | Same versions on every machine |
| `hello_puppy.py --report` + `scripts/sim_smoke_test.sh` (drafted and tested here) | `SIM SMOKE: PASS` headless, ~65 s |
| Refactor: `if __name__ == "__main__"`; IK and gait math in an importable module; unit tests without Isaac Sim | `pytest tests/unit` on GitHub CI |
| Record URDF→USD provenance (source URDF, converter version, manual edits) | `puppy_isaac/README.md` |

## P2 — Decentralized action-execution loop, no ROS yet (2 weeks)

| Deliverable | Acceptance |
|---|---|
| Body-velocity interface on the gait (`set_velocity(vx, wz)`) replacing fixed moves | Commanded vs. measured velocity within 30% |
| Simulated RGB camera on the PuppyPi | Image saved headless |
| Simple sim world (2 rooms, a door, a table, an object) matching the centralized example world | Loads in smoke test |
| Step Executor (in-process): accepts a step dict, maps `movetowards/search/inspect/push` → primitives, timeouts, one terminal result | Unit tests + sim test: `movetowards table` succeeds |
| **Rule-based** action selector (baseline) | Same sim test passes |

## P3 — Centralized dispatch → PuppyPi (1 week)

| Deliverable | Acceptance |
|---|---|
| `dispatch_adapter.py` (today's plan → contract v0.1) | Contract tests pass in both repos |
| Run a PuppyPi-only dispatch plan file in sim, step by step, honoring `depends_on` | A one-robot mission plan from `demo_pipeline.py`-style fixtures completes in sim |

## P4 — ROS 2 communication and feedback (2 weeks)

| Deliverable | Acceptance |
|---|---|
| `physical_ai_interfaces` ROS 2 package (Jazzy) | `colcon build` + generated messages match schemas |
| Isaac Sim ROS 2 bridge: `/clock`, `/cmd_vel`, `/odom`, `/tf`, camera | `ros2 topic hz` shows expected rates |
| Executor as a ROS 2 action server `/puppypi_1/execute_step` | Action round-trip with `ros2 action send_goal` |
| Execution Manager + World Model node in `centralized` wrapping the existing pipeline | Mission from natural language → dispatched → executed in sim → results logged |
| Replanning on `BLOCKED` / `FAILED` (orchestrator scope) | Fault-injection test: object moved → replan → success |
| Heartbeat / comms-loss / e-stop | Killing the executor yields `COMMS_LOST` and a replan |

## P5 — Local VLM inference and constrained action selection (2 weeks)

| Deliverable | Acceptance |
|---|---|
| Selector interface with two implementations: rule-based, VLM | Swappable by config, no contract change |
| JSON-schema/grammar-constrained decoding | ≥ 99% format-valid outputs on benchmark |
| Benchmark set of sim episodes with ground-truth decisions | Selector accuracy reported VLM vs. rule-based |
| Latency measured: workstation GPU; optionally Pi 5 CPU | Numbers in an experiment issue |
| Nav2 for `movetowards` (if not already) | Nav2 goal success in sim world |

## P6 — Drone and PX4 (3 weeks, highest technical risk)

| Deliverable | Acceptance |
|---|---|
| D7 simulator spike (Pegasus on Isaac Sim 6.1 vs. PX4+Gazebo) | Decision ADR |
| PX4 SITL + uXRCE-DDS + ROS 2; drone executor for `takeoff_from`, `movetowards`, `land_on`, `search` | Drone-only mission in sim |

## P7 — Coordinated heterogeneous missions (2 weeks)

| Deliverable | Acceptance |
|---|---|
| Drone searches, PuppyPi opens door / navigates (proposal scenarios: cooperative inspection, search-and-locate) | ≥ 20-mission benchmark; metrics from architecture §12 |
| Mission-level replanning (`scope: mission`) | Fault-injection recovery rate reported |

## P8 — Robustness and sim-to-real (remaining time)

| Deliverable | Acceptance |
|---|---|
| PuppyPi hardware: same ROS topics via Hiwonder's ROS 2 driver | Same executor runs unchanged |
| Drone hardware with safety pilot + geofence | Takeoff/move/land via executor |
| Sim-vs-real comparison on shared scenarios | Thesis chapter |

## Dependencies

```
P0 ─▶ P1 ─▶ P2 ─▶ P3 ─▶ P4 ─▶ P5
                         └──▶ P6 ─▶ P7 ─▶ P8
D7 decision must precede P6; D4/D5 hardware facts must precede P8 (and affect P2 scope: arm or no arm).
```

P5 and P6 can run in parallel with separate people once P4 is done.

---

## Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | No Isaac Sim 6.1-compatible PX4 integration (Pegasus lists up to 5.1) | High | High | D7 spike early; PX4+Gazebo fallback via ROS 2 |
| R2 | PuppyPi lacks an arm → `open/grab` impossible on hardware; missions in fixtures depend on them | Medium | High | Confirm D4 now; redesign scenarios around search/inspect/navigate; doors opened by scenario setup |
| R3 | Small VLM picks wrong actions too often | Medium | Medium | Rule-based baseline; closed menu; constrained decoding; this is a reportable result either way |
| R4 | Scripted gait doesn't transfer to the real PuppyPi (servo dynamics differ; gains are unmeasured) | High | Medium | Use Hiwonder's own gait on hardware behind the same `/cmd_vel` interface |
| R5 | Gemini API changes / quota / cost | Medium | Medium | Provider interface already exists; add an open local LLM option; offline fixtures |
| R6 | Isaac Sim upgrade breaks scripts (the API used is `isaacsim.core.experimental`) | Medium | Medium | Pin 6.1.0; smoke test gates any upgrade |
| R7 | Indoor drone positioning unavailable | Medium | High | Confirm D5; mocap/optical flow, or restrict drone to hover-and-inspect |
| R8 | Wi-Fi latency/loss with remote inference | Medium | Medium | Per-step (not control-rate) decisions; heartbeat + safe stop |
| R9 | Two simulators (if D7 → Gazebo) diverge in world state | Medium | Medium | Single World Model in the center; sims only report observations |
| R10 | Team time vs. scope (P0–P8 is a lot) | High | High | P7 in sim is the core thesis result; P8 hardware can be partial |
| R11 | Source-built Isaac Sim breaks when moved (happened on the lab workstation, 2026-11-08) | Realized | Medium | Prefer pip install; document path variable |

## Open research questions

1. Does a VLM selector outperform a rule-based selector on step success, and at what latency cost?
2. How much does constrained decoding improve validity without reducing decision quality?
3. How do LLM-, VLM- and (where applicable) VLA-based components compare in the same architecture (lit. review contribution 1)?
4. How much does event-driven replanning recover from injected failures, and at what token/latency cost?
5. What is the sim-to-real gap in step success for navigation and search on the PuppyPi?
6. When multiple robots observe conflicting facts, what merge policy keeps the world consistent with acceptable replan rates?
7. (From the lit. review §4) How should robots share observations in a way usable by an action-generating model?
