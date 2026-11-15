# Local mission dashboard

Start from the central repository with its existing dependencies installed:

```bash
.venv/bin/python mission_dashboard.py
```

Open **http://127.0.0.1:8787**. Enter a natural-language goal and select Run mission.
The dashboard is a local interface to the existing system, not a hosted service.
The Gemini key stays in the existing `.env`; no key is sent to the browser.

## What runs automatically

1. The original request is saved with a unique mission ID and the symbolic
   one-room world. Gemini plans tasks; the existing interface and orchestrator
   validate and produce dispatch JSON.
2. With VLM selected, the dashboard connects to `127.0.0.1:8765`. If unavailable,
   it launches the existing SmolVLM-Instruct server with workstation_gpu_live and
   the model weights already installed in the edge repository.
3. Isaac Sim opens visibly and executes the dispatch. VLM mode uses the existing
   experimental live visual controller; ground-truth mode uses the baseline map
   controller for comparison. This selection changes perception/execution, not
   the central planner's task contract.
4. Execution results are automatically validated and ingested into the central
   feedback snapshot, including when Isaac's launcher returns nonzero for a
   blocked mission. An absent report is an execution error, not fabricated feedback.
5. A halted mission triggers one reasoning-only replanning call. The proposal or
   clarification is shown for review. It is never activated automatically.

The page shows stages, the plan, execution outcomes, observation/review summaries,
replanning questions/tasks and the existing VLM live view. Expand the detailed
results or run log when needed. Starting a mission can make up to three bounded
Gemini calls (planning, orchestration and, on a halt, replanning). No automatic
model retry/format repair was added.

## Scope and controls

- This version starts fresh one-room PuppyPi simulations. It uses the existing
  example request's world and only changes its mission ID and natural-language
  instruction. It does not control an already-running stage or real hardware.
- The toy is deliberately absent from the scene. “Search living_room for the
  toy” exercises the failure/feedback path. “Find the apple” is another starting
  point, but live VLM navigation can be unreliable; use the test perception mode
  to distinguish controller issues from interface issues.
- Only one dashboard mission runs at a time. Stop run cancels its owned process
  group and prevents later stages. It leaves the VLM server available for other
  runs. During VLM loading, a canceled run can leave the server finishing startup.
- The server binds only to localhost and checks request Host/Origin for mission
  submissions. It serves no arbitrary files or `.env`. It is not a multi-user
  or network-facing service; do not expose its port to other machines.
- Existing Isaac Sim builds, the edge virtual environment, local model weights
  and the central Gemini environment are prerequisites. Model startup is limited
  to 150 seconds and simulator execution to 900 wall-clock seconds per run.
- Run data is stored in `outputs/dashboard/<run-id>/`: request, candidate,
  dispatch, simulator output, feedback, replan and logs. Latest finished state is
  restored on dashboard restart; active jobs are not resumed after a crash.

## Validation

118 central tests and 102 edge tests pass, including six dashboard tests covering
the coordinator with real feedback ingestion, clarification handling, failed
simulation exit codes, duplicate submissions, input validation, process-group
cancellation, restored finished state, and HTTP origin/file boundaries.

Desktop and phone browser checks verified rendering, controls and no horizontal
overflow. A real run submitted through the browser, `20261115-120013-c67cc5`,
automatically started the VLM, executed Gemini's plan in visible Isaac Sim,
succeeded at table approach, returned BLOCKED/OBJECT_NOT_FOUND for toy search,
ingested feedback and displayed Gemini's needs_clarification response. No new
revision was activated. Logs also retain an earlier stopped live-navigation run
and a planning-contract failure; these are surfaced in the interface.

A one-line CSS correction in the existing edge VLM view ensures elements marked
hidden remain hidden; this removes the stale “No frames yet” overlay after images
arrive. No inference, gait, navigation or planner algorithms changed for the UI.
