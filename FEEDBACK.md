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
Next work is an explicit revision/replanning contract consuming this snapshot,
preserving completed work and reconciling uncertain state before replacement
steps can run. ROS 2, live feedback transport and SmolVLM evaluation remain later work.
