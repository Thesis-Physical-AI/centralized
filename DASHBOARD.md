# Local mission dashboard

Start from the central repository with its existing dependencies installed:

```bash
.venv/bin/python mission_dashboard.py
```

Open **http://127.0.0.1:8787**. Enter a natural-language goal and select Run mission.
The dashboard is a local interface to the existing system, not a hosted service.
The Gemini key stays in the existing `.env`; no key is sent to the browser.

## What runs automatically

1. The original request is saved with a unique mission ID and the selected
   world (see *Worlds* below). Gemini plans tasks; the existing interface and orchestrator
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

## Worlds and run modes

The **World** card picks the environment the planner sees. Worlds are JSON files
in `worlds/`:

- `demo_living_room.json` is the original one-room PuppyPi demo. It is read-only
  in the dashboard (**Duplicate** makes an editable copy) and is the only world
  linked to an Isaac Sim scene (`"sim": {"world": "one_room.json"}` in
  `decentralised/sim/worlds/`).
- **New world** / **Edit** opens a form for entities (name, kind, located in or
  door connections) and robots (fleet id, start location), plus a JSON tab for
  extra fields (`weight_kg`, `open`, `passages`, …). Saving runs the same world
  validation the planner uses (`validate_world_context`), so robots must exist in
  `mission-orchestrator/robots.json`. A run keeps its own copy of the world, so
  editing or deleting a world never changes past or running missions.

The run mode below the mission chooses what happens after planning:

| Mode | After the dispatch plan |
|---|---|
| Isaac Sim | Steps 2–5 above (only for worlds with a sim scene). |
| External robot | Waits up to an hour for an execution report, then runs feedback and replanning. |
| Plan only | Stops; status *Plan ready*. Nothing is dispatched. |

For **External robot**, take the dispatch from the **Plan** dropdown, run it on the
robot, then paste the report into the dashboard or POST it from a local script:

```bash
curl -X POST http://127.0.0.1:8787/api/report -H 'Content-Type: application/json' \
  -d '{"job_id": "<run id>", "report": {"mission_id": "M-<run id>",
       "execution": {"mission_id": "M-<run id>", "plan_revision": 0, "status": "HALTED",
                     "steps": [{"mission_id": "M-<run id>", "plan_revision": 0, "step_id": "S1",
                                "robot_id": "puppypi_1", "attempt": 1, "status": "SUCCEEDED",
                                "reason_code": "", "observations": []}]}}}'
```

The report uses the same contract as `decentralised/run_prototype.py`'s
`results.json`. `/api/report` is the only endpoint that accepts requests without
a browser Origin (local scripts); a foreign Origin is still refused.

## Scope and controls

- Isaac Sim runs start a fresh simulation of the world's sim scene. The dashboard
  does not control an already-running stage; real robots report back through
  External robot mode.
- In the demo, the toy is deliberately absent from the scene. “Search living_room for the
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
