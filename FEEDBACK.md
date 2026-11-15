# Execution feedback snapshot

The central layer can now ingest a complete saved executor report, validate its
correlation with the original dispatch, and emit a detached feedback snapshot.
This is the stopping point before feedback-driven replanning: it does not call
Gemini, dispatch commands, or replace an active plan.

From this repository root, using an environment with the declared dependencies:

```bash
.venv/bin/python -m structured_mission_interface.feedback \
  --dispatch ../decentralised/missions/dispatch_one_room.json \
  --report ../decentralised/runs/dispatch-visible-20261115/results.json \
  --plan-revision 0 \
  --output outputs/feedback-visible-20261115.json
```

Output paths must be new; existing files are never overwritten. Exit 0 means
ingestion succeeded, even when the recorded mission is HALTED. Exit 2 indicates
invalid input or an output error. The expected revision is supplied explicitly
because the original central dispatch format has no execution revision.

## Validation boundary

Input dispatches must already have passed the central planning validators. The
feedback reader validates initial/final world consistency with the fleet and
requires every step to appear exactly once in the full report. It checks mission,
revision, robot, first-attempt identity, contract version, terminal statuses,
reason codes, observation identities and unique event IDs. It preserves implicit
per-robot ordering and cross-robot dependencies, rejecting reports that claim
execution before successful prerequisites. Manager NOT_DISPATCHED entries remain
bookkeeping, not fabricated robot feedback.

The visible simulator's nested `execution` report and a standalone manager report
are accepted. Nested execution data is authoritative; display metrics and the
outer duplicate step list are not used for world updates. Replaying the same
report recomputes the same snapshot from the original dispatch. This is not a
streaming/persistent ingestion service, and does not maintain a revision ledger
across invocations. Attempt numbers other than 1 are deliberately rejected until
retry semantics are implemented.

## Conservative world policy

- Successful move/inspect results update symbolic robot location when the target
  resolves unambiguously to a room. Coordinates are not inferred from end_pose.
- Failed actions may have partial effects. Their previous symbolic state remains
  last-known state, with a reconciliation notice in `review`; it is not a verified
  current pose.
- Successful search sightings can fill a missing entity location only for
  non-VLM evidence with confidence exactly 1.0 matching the dispatched object and
  search area. This initial policy supports the ground-truth integration test;
  it is not an authentication or general perception-confidence guarantee.
- Coarse sightings never overwrite a finer known location. For example, finding
  an apple in living_room preserves apple in table when table is in living_room.
- Conflicting sightings, VLM evidence, unimplemented observation types, and
  unimplemented successful action effects are recorded for review, not applied.
- Negative searches retain the entity and its last-known location. They record
  search evidence; they do not prove physical absence or delete the entity.

`tasks` summarizes execution of each task's declared steps as COMPLETED,
NEEDS_REVIEW, IN_PROGRESS or NOT_STARTED. COMPLETED does not independently prove
the task's natural-language goal. `replanning_required` flags a halted execution;
the original revision is retained, not incremented. Review notices must be
resolved by the next feedback/replanning stage before trusting the world as current.

## Verification and current checkpoint

106 central tests pass (44 interface/dispatch/feedback tests plus 62 reasoning
tests), including eight new feedback tests with malformed-input subcases.
They cover successful updates, negative evidence, conservative/conflicting
locations, multi-robot prerequisites, duplicate/stale results, partial failure,
input immutability, deterministic replay and CLI overwrite protection.

The saved visible Isaac Sim run was replayed successfully. Its snapshot is at
`outputs/feedback-visible-20261115.json` (git-ignored local output): PuppyPi's
last successful location is crate; apple remains in table; toy remains in the
world with failed-search evidence; T1 is COMPLETED, T2 NEEDS_REVIEW and T3
NOT_STARTED. Mission status is HALTED and replanning_required is true.

Existing planning APIs, dispatch format, robot execution, VLM and simulator code
are unchanged by this feedback increment. No new package dependencies were added;
the central virtual environment was populated from the existing pyproject ranges.
Reasoning-only replanning preparation is now available below. Active-plan
replacement and reconciliation before new execution remain future work. ROS 2,
live feedback transport and SmolVLM evaluation also remain later work.

## Feedback into the reasoning layer

The new `reasoning-layer/replan_mission.py` entry point connects saved execution
feedback to MissionPlanner.replan. It recomputes feedback from the original
dispatch/report, rather than blindly accepting an editable snapshot. It checks
the dispatch mission/world against the original mission request, requires a
halted report with matching expected revision, then supplies the planner with:

- Original natural-language goal and the updated, validated world.
- Previous step actions/arguments and their results, including partial successes.
- Task execution summaries and completed task IDs.
- Observations, failed-search evidence and reconciliation/review notices.
- Source revision and proposed next revision.

The initial planner/orchestrator pipeline remains unchanged: feedback enters
through this explicit new command, not a background watcher or live robot topic.
The original `plan_mission.py` and `run_mission.py` do not automatically replan.

Preview the exact reasoning input without an API call:

```bash
.venv/bin/python reasoning-layer/replan_mission.py \
  --input reasoning-layer/examples/replanning_one_room_request.json \
  --dispatch ../decentralised/missions/dispatch_one_room.json \
  --report ../decentralised/runs/dispatch-visible-20261115/results.json \
  --plan-revision 0 --dry-run \
  --output outputs/replanning-visible-preview-20261115.json
```

The example request is a hand-authored description for the existing hand-authored
one-room fixture. Its world matches that dispatch. It is not a recovered original
Gemini prompt; production replanning must supply the actual original request.
The saved preview includes completed T1, failed toy-search evidence, pending S5,
the unchanged apple-on-table relation and the last successful robot location crate.

Without `--dry-run`, the command makes at most one bounded Gemini generation
call using the existing API configuration and limits. It returns a review proposal
containing the decision, candidate handoff, execution context, interface warnings,
source revision and proposed revision. Ready candidates pass the existing
Structured Mission Interface against the updated world. Clarification or
infeasibility returns no handoff. Output files are new-only; CLI exit codes are
0 for preview/ready, 3 for clarification/infeasibility, and 2 for errors.

New task IDs must start with the proposed revision prefix, such as `R1_T1`.
Completed tasks remain history, not dependencies on absent tasks in a new graph.
The model is instructed to preserve remaining goals and avoid repeating completed
work. This is not a semantic proof: a new ID with a repeated description can
still require reviewer correction. Review notices remain visible; no algorithm
claims to have reconciled uncertain physical state.

Every proposal is marked requires_review and sends_robot_commands=false. It does
not call the orchestrator, activate a new revision, cancel old work, or execute
replacement steps. Those transitions need the next integration stage; the flag
is proposal metadata, not a system-wide authorization mechanism.

Verification: all 112 central tests pass (44 core tests and 68 reasoning tests,
including six new replanning tests). Mocked generation confirms history reaches
the reasoning prompt, invalid feedback stops before generation, revision task
IDs are checked, repairs retain feedback, and clarification stays non-dispatchable.
The standalone dry run also succeeds outside the repository working directory.
The actual visible simulator report was ingested into the saved preview; no live
Gemini call, orchestrator replan, or additional simulator run was performed for
this increment.

## Live sequence verification — 15 November 2026

A subsequent live test completed three Gemini generations: initial reasoning,
initial orchestration, and feedback-driven reasoning. The initial dispatch
passed the existing validation gates and ran in the visible Isaac Sim app with
ground-truth perception. Apple search succeeded; the deliberately absent toy
search returned BLOCKED/OBJECT_NOT_FOUND; table approach and inspection were
withheld. The central feedback snapshot marked T1 completed and the mission halted.

Gemini's revision-1 proposal omitted the completed apple search, retained the toy
search and subsequent table inspection, used R1-prefixed task IDs, and passed the
Structured Mission Interface against the feedback world. This verifies the live
planning-to-simulation-to-feedback-to-reasoning path. It does not establish an
adaptive recovery strategy: the proposal still asks to search for the toy without
specifying a changed strategy. No revision-1 orchestration, plan activation or
second execution was performed, and SmolVLM was not used in this test.

Local artifacts are `outputs/live-sequence-{candidate,dispatch,feedback,replan,summary}-20261115.json`
and `../decentralised/runs/live-sequence-20261115/`. All are local run artifacts,
not committed fixtures. The summary records the step outcomes, proposed tasks
and verification limits. Missing-toy recovery remains a question for the next
orchestration/recovery stage; a ready task proposal alone is not mission success.

### Critical replanning prompt check

The reasoning system prompt now requires an evidence-supported material change
before proposing a retry of failed work. It distinguishes failure from unattempted
downstream work, separates verified state from last-known state, preserves partial
progress, and explains that negative views/full turns do not establish exhaustive
room coverage. A ready recovery task must carry the change and its basis in its
description/requirements. Missing essential evidence calls for clarification;
infeasible requires evidence of an unattainable required goal.

One live comparison reused the same original request, generated dispatch and
simulator results. The earlier prompt returned ready with another same-area toy
search. The strengthened prompt returned needs_clarification, asked for another
search area or confirmed target location, and produced no handoff. The new result
is `outputs/live-sequence-critical-replan-20261115.json`. The CLI returned 3 as
documented for clarification. No replacement plan was activated or executed.
All 68 reasoning tests still pass; no schema or executor changes were needed.
This single comparison demonstrates the desired behavior on this incident, not
a deterministic or statistical guarantee. Semantic retry checking still depends
on model reasoning and review, not an implemented enforcement gate.
