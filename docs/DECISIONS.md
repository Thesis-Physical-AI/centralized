# Architectural Discrepancies and Decisions Requiring Confirmation

Each item lists the conflict, the evidence, options, and a recommendation. Items marked **Supervisor** change the academic framing and should be confirmed with Prof. ElBatt; **Team** items can be decided internally. Once decided, each becomes an ADR in `docs/adr/`.

## A. Discrepancies found

| # | Discrepancy | Sources in conflict | Resolution proposed |
|---|---|---|---|
| X1 | Mission-level model: OpenVLA (proposal) vs. "LLM/VLM/VLA, specifically OpenVLA" (lit. review) vs. Gemini (report, code) | Proposal §2; lit. review §5.1.1; code | D1 |
| X2 | Robot-level intelligence: none (proposal/review: PX4/Nav2 executors) vs. SmolVLM decision-maker (decentralised diagram) | Lit. review §5.1.3; `architecture.dot` | D1 — adds a layer the review does not describe; document as an extension |
| X3 | Fleet: drone + PuppyPi (proposal) vs. + TurtleBot (`robots.json`, examples, tests) | Proposal §2; `robots.json` | D3 |
| X4 | PuppyPi manipulation (`open`, `grab`, …, 0.2 kg payload) assumed in code; arm and payload not documented anywhere | `robots.json`; vendor listings show arm only in Pro/Ultimate kits | D4 |
| X5 | Step message field `id` (centralized) vs. `step_id` (decentralised diagram) | `dispatch_plan.json`; `architecture.dot` | Contract v0.1 uses `step_id`; adapter maps `id` |
| X6 | Step results return an "updated world" (`SKILL.md`, diagram) — lets robots overwrite global state | `SKILL.md` §Replanning; diagram | Contract §6: robots send observations; center owns world |
| X7 | Lit. review Fig. 1: robot executors "enforce platform-specific safety policies … protection against malicious attacks" — no design exists for the security part | Lit. review §5.1.3 | Scope security to input validation + action allow-lists; state this explicitly in the thesis |
| X8 | Decentralised README states it "runs on the PuppyPi"; currently it runs only on the workstation in Isaac Sim | README | Reword to "targets the PuppyPi; currently simulation only" |
| X9 | Install docs assume `~/Projects/isaacsim` and a source build; actual path differs and the build was not relocatable | `install.sh`, README, `command.txt` | D8 + `ISAACSIM_PATH` discovery |
| X10 | Proposal Milestone 2 asks to "Connect OpenVLA reasoning layer to the robot autonomy stack" and "implement ROS 2"; Progress Report 1 shows the center ends at a JSON file | Proposal; report | Roadmap P3–P4 |
| X11 | Proposal M3 says "Re-use Cyberscape Project Outcome from AY2025-2026"; no reference to Cyberscape in either repo or the review | Proposal M3 | **Ask supervisor** what Cyberscape provides and whether it is still expected |
| X12 | Nav2 for PuppyPi (proposal) requires odometry/localization; sim robot has no sensors yet; real PuppyPi lidar exists only in some kits | Proposal; vendor | D4 + roadmap P2 |

## B. Decisions

### D1 — Where the VLA sits, and what fills its slot today (Supervisor)

**Team position:** a VLA maps one robot's observations and an instruction to that robot's actions, so it belongs on the decentralized agents — not in the center, where the proposal places OpenVLA. The center does cross-robot planning, which is an LLM task. No available VLA currently runs on our edge hardware *and* supports a quadruped or a quadrotor, so each agent uses a VLM in the VLA slot, choosing from a closed action list, with classical executors producing motion.

What the supervisor is asked to confirm: that moving the VLA role from the center to the agents, and filling it with a VLM until an edge-deployable VLA exists, is acceptable for the thesis.

- **Option A (team choice):** Center = LLM (Gemini, swappable). Agents = VLM in the VLA slot + classical executors (same layering NaVILA uses for legged robots). A VLA (SmolVLA) evaluated for manipulation if PuppyPi has an arm. Agent interface kept model-independent so a VLA can replace the VLM later.
- Option B: Fine-tune OpenVLA for PuppyPi/drone. Requires demonstration data on platforms OpenVLA was not trained on; ~15 GB GPU memory in bf16, ~6 Hz on an RTX 4090 — not deployable on the robots. High risk for the timeline.
- Option C: Keep OpenVLA in the center as the proposal states. Rejected: OpenVLA outputs single-arm actions for one robot and does not decompose missions or allocate robots.

Evidence: `MODEL_SELECTION.md`.

### D2 — Where robot-level inference runs (Team)

Recommended: workstation over ROS 2 for sim and first hardware; measure SmolVLM-256M on a Pi 5 CPU before considering onboard. Selector decisions are per step (seconds), not control-rate.

### D3 — Is TurtleBot in scope? (Supervisor + Team)

- Keep as **simulation-only** third robot (it exercises carrier/heavy-object logic already tested), clearly labelled; or
- Remove from the default fleet to match the proposal, keeping it in a test fixture.

Recommendation: keep in tests/fixtures, remove from the default mission fleet until AUC hardware or a sim model exists.

### D4 — PuppyPi hardware configuration (Team, needs lab check)

Record exactly: kit (Standard / Advanced / Pro / Ultimate), Raspberry Pi model and RAM, camera, lidar (yes/no, model), arm (yes/no, DOF), measured payload. Until confirmed, `open/close/grab/put*` on `puppypi_1` are **hypothetical** and must be flagged in `robots.json` (e.g. `"status": "unverified"`).

### D5 — Drone hardware (Team, needs lab check)

Airframe, flight controller and PX4 version, companion computer, camera, indoor positioning (none / optical flow / mocap). Indoor flight without positioning makes `movetowards` unreliable.

### D6 — Where shared contracts and the system architecture live (Team)

**Interim (2026-11-08):** the documents live in `centralized/docs/` and `decentralised` links to them (Option B below), so there is already one copy. Revisit when the ROS 2 interface package is created.

- **Option A (recommended):** New small repo `Thesis-Physical-AI/interfaces` holding `SYSTEM_ARCHITECTURE.md`, ADRs, JSON Schemas and the ROS 2 interface package. Both repos pin it (git submodule or tagged release). One home, versioned, no duplication.
- Option B: Keep them in `centralized/docs/`; decentralised references by link and vendors the ROS 2 package. Fewer repos, but the decentralised build depends on a file in the planning repo.
- Option C: Monorepo. Largest change; not recommended now.

### D7 — Drone simulator (Team)

- A: Pegasus Simulator inside Isaac Sim — latest listed release targets Isaac Sim 5.1; porting to 6.1 is unverified work.
- B: PX4 SITL with Gazebo for the drone, joined to Isaac Sim robots only through ROS 2 (two simulators, two worlds to keep consistent).
- C: Isaac Sim 6.1 + PX4 SITL via a custom MAVLink/uXRCE bridge.

Recommendation: spike A on 6.1 for ≤ 3 days; fall back to B for the multi-robot milestone if it fails. Decide before P6.

### D8 — Isaac Sim install method (Team)

- A (recommended to evaluate): `pip install isaacsim[all,extscache]==6.1.0.0` from `pypi.nvidia.com` in a dedicated venv — versions 6.0.0–6.2.0 are published (verified). No source build, relocatable, pinnable.
- B: Keep source build at a documented path, located via `ISAACSIM_PATH`.

Both should work behind one `ISAACSIM_PYTHON` variable so scripts don't care.

### D9 — Python tooling (Team)

Centralized uses `uv`; decentralised uses `venv + pip`; Isaac Sim brings its own Python 3.12. Recommendation: `uv` for both non-Isaac environments (lockfiles), Isaac Sim's Python for simulation, ROS 2 Jazzy's system Python 3.12 for ROS nodes. Document all three in one table.

### D10 — Contract evolution policy (Team)

Semver for `contracts/VERSION`; breaking changes need an ADR and a coordinated release of both repos.
