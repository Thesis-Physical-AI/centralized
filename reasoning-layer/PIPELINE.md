# Connected mission planning pipeline

`run_mission.py` connects the two planning agents at runtime:

1. The reasoning agent generates high-level candidate tasks.
2. A minimal Mission Interface gate validates the task contract, dependency graph,
   mission identity and unchanged world snapshot.
3. Gemini receives the existing `mission-orchestrator/SKILL.md`, the accepted
   candidate and the fleet definition, then generates allocations and action steps.
4. The application checks dispatch coverage/dependencies and runs the existing
   `mission-orchestrator/validate_plan.py` simulation checks.
5. Only a complete plan with no errors or warnings is published as dispatch JSON.

This command generates a plan file. It does not communicate with ROS 2 or send
robot commands. The interface gate is an initial structural implementation;
human approval, permissions and full semantic/policy validation are not implemented.

## Run from the repository root

Use the same `.env` and locked environment as the reasoning-only CLI:

```bash
uv sync --locked
uv run python reasoning-layer/run_mission.py \
  --input reasoning-layer/examples/request.json \
  --handoff-output outputs/candidate-002.json \
  --output outputs/dispatch-002.json
```

The default full run makes at most **two generation calls**, one per agent, with
no retries or format-repair loop. Each call has the same default 2,048-output-token,
30,000-prompt-character and 45-second limits. `--max-output-tokens`,
`--max-prompt-chars` and `--timeout-ms` configure those per-call limits. Token usage
is printed on stderr, including completed calls before a later failure.

Keep billing disabled on the API key's project for free-tier-only use. These
local budgets do not inspect remaining quota or billing settings. A quota error
stops the run; the application does not switch models or retry it.

To inspect inputs and limits without API calls:

```bash
uv run python reasoning-layer/run_mission.py \
  --input reasoning-layer/examples/request.json --dry-run
```

## Reuse an existing candidate

Skip the reasoning call by providing a saved candidate. The request's mission ID
and world must still match that candidate. This is not feedback-based replanning;
a stale candidate or changed world is rejected.

```bash
uv run python reasoning-layer/run_mission.py \
  --input reasoning-layer/examples/request.json \
  --handoff reasoning-layer/examples/expected_handoff.json \
  --output outputs/dispatch-from-candidate.json
```

This mode makes at most **one generation call**. The same option can reuse a real
candidate saved by `plan_mission.py` or `--handoff-output`.

Output filenames must be new. An optional `--handoff-output` is written immediately
after the interface gate passes, so it can survive an orchestration failure and
be reused without another reasoning call. The final `--output` file is written
only after dispatch validation passes. Omit `--output` to print the dispatch JSON.

## Dispatch contract and validation

The final file follows the existing orchestrator format: `mission_id`, `world`,
`allocations`, `steps` and `unassigned`. Identity and world are copied by the
application. Gemini generates only allocations, action arguments and dependencies;
the application renders `instruction` from the fleet's action syntax so it cannot
disagree with the checked action/arguments. The existing skill file is loaded for
each invocation and its allocation/sequencing procedure remains the agent prompt.
The repository's input/dispatch example is supplied as labelled few-shot context,
adapted to the same runtime fields and dependency policy. It is not selected or
returned as a fixed solution; generated plans are checked against the current
mission and supplied fleet.
An absent, invalid or outdated worked example is skipped rather than preventing
operation with a changed deployment fleet.

Checks include exact task coverage, known allocated robots, matching task/robot
steps, valid entity references, argument syntax, dependency cycles, and the existing
simulator's preconditions. Used robots need a known initial room.

The runtime uses a conservative task-dependency rule: **every step of a prerequisite
task completes before any step of a dependent task**. This includes cleanup steps
such as landing. Independent tasks can still run in parallel. This is stricter
than the original example's search-result dependency; it avoids assuming a
task-completion event that the current contract does not explicitly represent.
The adapter adds these barriers deterministically to the generated steps, while
preserving existing physical dependencies. If this exposes a contradictory
per-robot order or a cycle, validation rejects the candidate instead of reordering
robot actions or making an automatic model retry.

Any validator warning holds the candidate for review. A response containing
unassigned tasks produces a report rather than a partial dispatch file. This is
stricter than manually accepting warnings with a written rationale in the skill.

| Exit code | Meaning |
| --- | --- |
| 0 | Validated dispatch produced, or dry run completed |
| 2 | Input/API/output failure or rejected dispatch, including warnings |
| 3 | Mission needs clarification, is infeasible, or contains unassigned tasks |

On failure, inspect the report on stdout and diagnostics on stderr. There is no
automatic repair call. Reuse a saved accepted candidate to investigate the
orchestration stage without repeating reasoning.

## Scope of the checks

The existing validator simulates a dependency ordering in a simplified world.
Passing it does not prove that every parallel interleaving is safe, that a task's
natural-language goal is achieved, or that a real robot can execute the plan.
Execution feedback, observation-driven world updates, mission-level replanning,
and actual executor integration remain separate work.

## Offline tests

```bash
uv run python -m unittest discover -s reasoning-layer/tests -t reasoning-layer -v
```

Pipeline tests mock the two model responses while exercising the actual interface
gate, orchestration adapter, instruction renderer, dispatch contract checks and
existing simulator validator. The test dispatch adapts the original example's
search dependency to the stricter task-completion barrier; the original example
and validator are left unchanged.
