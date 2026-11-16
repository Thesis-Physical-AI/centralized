# Persistent execution and recovery revisions

The dashboard can now continue a mission in the **same Isaac Sim session** after
a blocked plan. The dashboard enables this for Isaac Sim runs. Legacy one-shot
CLI missions and plan-only/external-report modes retain their existing behavior.

## Lifecycle

1. Save the original request, objective constraints and revision-0 dispatch in
   `lifecycle.json`. Stable objective IDs are distinct from revision task IDs.
2. Start Isaac Sim once with `--session-dir`. Each completed revision publishes
   its report and idle checkpoint: mission/session identity, revision, measured
   pose, simulation time, safety state and a single-use token.
3. Pause physics while keeping the app responsive. Neither the robot nor the
   scene is reset while reasoning runs or the operator answers a question.
4. Reconcile symbolic robot location from the paused physical pose. This initial
   policy requires a safe robot inside a single known room. Retain a precise
   previous target only when its approach pose is nearby; otherwise use the room.
   This never changes object locations or physical scene geometry.
5. Reason about the original goal with validated history, completed work,
   remaining objectives, observations, review notices and user answers. A ready
   candidate can be orchestrated and validated through the existing gates.
6. When activation gates pass, request a fresh pause acknowledgment. It returns
   new telemetry and a new checkpoint token. Changed physical state invalidates
   the candidate; it is not executed against stale assumptions.
7. Activate exactly source revision + 1 with that token. The edge checks identity,
   revision, idle state, safety and token before accepting. The central active
   revision changes **only after the matching activation acknowledgment**.
8. Execute with the same simulator, navigator, camera and robot instance, then
   ingest feedback again within the recovery budget. Success closes the session.
   Stop or an unrecoverable error terminates the owned simulator.

## Activation gates

- Handoff/dispatch must match the original mission and current reconciled world.
  Existing schema, capability, action, graph, constraint and symbolic execution
  checks remain in force. Dispatch warnings prevent activation.
- `objective_coverage` maps every new task to stable original objective IDs. It
  must cover **all** unfinished objectives and **none** of the completed ones.
  New task IDs carry the next revision prefix. Completed and partially verified
  work is retained across the full history, not only the latest report.
- Executable goal predicates are checked too: a missing toy search cannot become
  an easy crate inspection. Already verified search/inspection predicates cannot
  be repeated as new work. Original explicit action, robot and payload constraints
  remain enforced. This does not prove arbitrary natural-language goal semantics.
- Local replacement execution supports only PuppyPi's `movetowards`, `search`
  and `inspect`. Unsupported peer/action work is rejected, never silently dropped.
  Map navigation targets require a known approach pose.
- Failed primitives cannot simply repeat. A changed search needs a supported
  approach/viewpoint at least 0.3 m from the current pose and relevant failed
  search poses. The check accounts for search itself navigating to its area;
  returning to the old area after an intermediate move is not a new view.
  Failed searches are tracked per target: visiting a table for an apple does
  not exhaust that viewpoint for the toy. This is a prototype no-progress
  heuristic, not a geometric visibility or collision guarantee.
- Negative evidence never deletes entities. Measured pose can resolve notices
  about pure motion's partial effects; other unresolved observation/state review
  requires explicit operator confirmation. Confirmation cannot bypass identity,
  objective coverage, capabilities, no-progress checks or recovery limits.

The default limit is **three activated replacement revisions**, with at most five
clarifications and 5,000 total clarification characters. Initial planning uses two
Gemini calls. Each recovery proposal uses one reasoning call plus one orchestration
call if ready; clarification decisions use reasoning only. Existing per-call
SDK limits apply. Model failures do not start an automatic retry loop.

## Dashboard controls

For Isaac Sim, **Continue validated recoveries automatically** controls activation.
Turning it off still keeps the session alive for manual review. Choose it before
starting. While **Waiting for your answer** or **Recovery review** is shown, the
robot stays paused and a new mission cannot take over the session.

Answer in the recovery panel and select **Send answer & replan**. Answers are
recorded separately, not substituted for the original mission or applied as
physical object moves. If unresolved observation review is the only blocker,
confirm your review and select **Activate validated revision**. Other gate
failures need a different candidate. Input is bound to both source revision and
proposal ID, preventing an old tab from approving a newer unseen proposal.

`/api/recovery` queues input on the mission worker; HTTP threads never access
simulation objects. External-report and plan-only modes do not yet have revision
activation transport, so they retain proposal-only behavior after feedback.

## Implementation and artifacts

- `mission_lifecycle.py`: persistent ledger, objective coverage, pose reconciliation,
  activation checks, acknowledgment handling and local session client.
- `reasoning-layer/reasoning/replanning.py`: revalidates historical reports and
  prepares bounded reasoning context. Optional `replan_mission.py --history ...
  --clarification ... --dispatch-output ...` also orchestrates a replacement;
  the CLI itself does not activate it.
- `decentralised/puppy_agent/session.py`: edge revision state machine/mailbox.
- `decentralised/run_prototype.py --session-dir <new-directory>`: persistent
  main-thread simulator loop. Without this option execution remains one-shot.

Each run retains `lifecycle.json`, proposal artifacts under `revisions/` and:

```text
session/status.json           physical session and checkpoint
session/commands/<id>.json    atomic pause / activate / close command
session/acks/<id>.json        accepted or rejected acknowledgment
session/reports/<rev>.json    per-revision execution report
```

Command IDs match filenames. Old results are validated and archived under `late/`,
not applied to the active world. Conflicting duplicate reports and recycled
observation IDs are rejected. Reports use attempt 1 within each new revision:
recovery creates new revision-scoped assignments, not blind retries of old ones.
Proposal, operator confirmation and activation acknowledgment are retained in
the ledger. Transaction state is saved atomically before activation; absent or
rejected acknowledgments never count as an active switch and are not blindly resent.

The mailbox is trusted-local development transport, not a network service or
ROS 2 action replacement. A new simulator needs a fresh directory. Crash
reconnection/resumption, multi-room reconciliation, fleet-wide activation, real
hardware cancellation and general world-state reconciliation remain separate
work. Existing heterogeneous planning support is preserved.

## Verification — 16 November 2026

A visible ground-truth continuity test approached the table, stopped after the
absent-object failure, accepted a hand-authored revision, and completed inspection.
Session identity was unchanged and revision 1 started at exactly the stopped pose:
`[0.7072486281, 0.6942645907, -0.7401771990]`.

Live dashboard trial `20261116-113829-3c523e` used Gemini and ground-truth execution.
The initial room-navigation action was rejected because this demo room has no
approach pose. Feedback generated a different valid revision, automatically
activated in the original scene. It found the apple and then failed to find the
absent toy near the crate. A dashboard clarification produced a revision-2
candidate preserving the completed apple objective. A conservative draft view
guard held that candidate; the corrected guard passed replay against the saved
candidate. The trial was stopped, rather than executing revision 2.

Summary: `outputs/recovery-validation-20261116.json`. Full live artifacts are in
the named dashboard run; continuity artifacts are under
`/tmp/physical-ai-revision-test-20261116/`. This verifies session continuity, live
activation and clarification ingestion, not success on an absent toy or a fully
autonomous multi-robot/hardware deployment.

Offline tests cover clarification → orchestration → activation → feedback with
the real mailbox and a stub physical actor, manual mode, stale proposals/results,
goal preservation, limits, fresh tokens, physical drift and session reset rejection.
The corrected object-specific viewpoint guard has a regression test. No new
dependencies were added.
