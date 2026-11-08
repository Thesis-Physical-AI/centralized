# Documentation-Driven Workflow for Humans and AI Coding Agents

## 1. Principle: each fact lives in exactly one place

| Information | Single home | Everything else does |
|---|---|---|
| System vision, layer responsibilities, model roles, status | `centralized/docs/SYSTEM_ARCHITECTURE.md` (D6 interim) | links to it |
| Message schemas, IDs, states, reason codes | `centralized/docs/INTERFACE_CONTRACTS.md` now; JSON Schemas in `contracts/` once created (planned) | links to it |
| Why a decision was made | `docs/adr/NNNN-*.md` | links to the ADR |
| How to set up / run / test one repo | that repo's `README.md` + `docs/DEVELOPMENT.md` | — |
| Rules for agents working in a repo | that repo's `AGENTS.md` | `CLAUDE.md` is a one-line pointer to `AGENTS.md` |
| Component status | the status table in `SYSTEM_ARCHITECTURE.md` §13 | READMEs say "see status table" |

`AGENTS.md` is the cross-tool convention read by Codex, Cursor, Gemini CLI, Copilot and others; Claude Code reads `CLAUDE.md`. To avoid duplication, `CLAUDE.md` contains only `@AGENTS.md` (Claude Code's import syntax), so both tools read the same text.

## 2. Rules every agent follows (the substance of AGENTS.md)

1. **Read first.** Before changing code, read `SYSTEM_ARCHITECTURE.md` §4 (responsibilities) and the contract for any interface you touch.
2. **Stay in your layer.** Do not move a responsibility between layers (e.g. allocation into the reasoning layer, planning into an executor) without an accepted ADR.
3. **Contracts are not edited casually.** Changing a schema, message, ID, state or reason code requires: an ADR, a `contracts/VERSION` bump, updated examples, and passing contract tests in *both* repos.
4. **Status honesty.** Never mark a component ✅ without a test or reproducible run. Never describe planned work as implemented in docs, comments, commit messages or PR text.
5. **Discrepancies are reported, not resolved silently.** If docs and code disagree, stop, describe both, propose a resolution in the PR description or an issue labelled `discrepancy`.
6. **Preserve tests.** Do not delete or weaken an existing test to make a change pass. If a test is wrong, say why in the PR.
7. **No new frameworks** (languages, middleware, ML libraries, build systems) without an ADR.
8. **No large downloads in default paths.** Model weights and Isaac Sim assets are opt-in.
9. **Secrets.** Never commit `.env`, API keys, or tokens.
10. **Run the checks** listed in the repo's AGENTS.md before declaring a task done, and paste the result summary in the PR.

## 3. ADRs

Format: Michael Nygard style, one page — *Context, Decision, Status (Proposed/Accepted/Superseded), Consequences, Alternatives*. Initial set to write from `DECISIONS.md`:

| ADR | Title |
|---|---|
| 0001 | Record architecture decisions; architecture document is the source of truth |
| 0002 | Hybrid centralized/decentralized split and layer responsibilities |
| 0003 | Model roles: LLM in the center; VLA slot on the agents, filled by a constrained VLM until an edge-deployable VLA exists (D1) |
| 0004 | ROS 2 Jazzy actions for step execution; contract v0.1 |
| 0005 | Center owns world state; robots report observations |
| 0006 | Isaac Sim 6.1 pinned; install method (D8) |
| 0007 | Location of shared contracts (D6) |

## 4. GitHub workflow

- Branches: `feat/…`, `fix/…`, `docs/…`; PRs to `main`, one reviewer from the team; squash merge.
- Issue templates: **Feature / task**, **Bug**, **Discrepancy** (docs vs. code), **Experiment** (hypothesis, metric, result).
- PR template checklist: layer(s) touched; contract changed? (then ADR + version); status table updated?; tests run (paste output); no planned-as-implemented claims.
- Labels: `layer:reasoning`, `layer:interface`, `layer:orchestrator`, `layer:executor`, `layer:sim`, `contract`, `discrepancy`, `experiment`, `hardware`.

## 5. Continuous integration

| Repo | Job | Runs on | Needs GPU? |
|---|---|---|---|
| centralized | `uv sync --locked`, both unittest suites, `demo_pipeline.py` | GitHub-hosted Ubuntu | No |
| decentralised (planned, needs unit tests first) | lint (ruff), pure-Python unit tests (IK, action mapping, contract validation) | GitHub-hosted Ubuntu | No |
| decentralised | Isaac Sim headless check (`hello_puppy.py --headless --check` today; stricter `scripts/sim_smoke_test.sh` planned) | **Manual / self-hosted runner on the lab workstation** | Yes |
| both (later) | contract tests against the pinned `contracts/` version | GitHub-hosted | No |

GitHub-hosted runners have no NVIDIA GPU, so the Isaac Sim check runs locally or on a self-hosted runner; the PR template asks for its output when sim code changes.

## 6. Where the workflow files are

| File | centralized | decentralised |
|---|---|---|
| `AGENTS.md` (rules + checks for that repo) | ✅ | ✅ |
| `CLAUDE.md` (`@AGENTS.md`) | ✅ | ✅ |
| `.github/pull_request_template.md` | ✅ | ✅ |
| `.github/ISSUE_TEMPLATE/{task,bug,discrepancy,experiment}.md` | ✅ | ✅ |
| `.github/workflows/ci.yml` | ✅ | 📋 after unit tests exist |
| `docs/adr/0000-template.md` | ✅ | — (ADRs live with the architecture) |
