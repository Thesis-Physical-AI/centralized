# AGENTS.md — centralized

Instructions for AI coding agents (and humans) working in this repository.

## Read before changing anything
1. System architecture (source of truth for **both** repos): [`docs/SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md) — §4 layer responsibilities, §6 model roles, §13 status.
2. Interface contracts: [`docs/INTERFACE_CONTRACTS.md`](docs/INTERFACE_CONTRACTS.md) — required for any change to dispatch plans, steps, results or world state.
3. Open decisions and known discrepancies: [`docs/DECISIONS.md`](docs/DECISIONS.md). Don't implement around an open decision; ask.
4. Workflow rules (ADRs, PRs, status honesty): [`docs/AGENT_WORKFLOW.md`](docs/AGENT_WORKFLOW.md).
5. `ARCHITECTURE_REVIEW.md` — what has been validated and how.

## What this repo owns
Layers ① Reasoning & Mission Planning, ② Structured Mission Interface, ③ Mission Orchestration, and (planned) ④ Execution Manager and ⑤ World Model. It does **not** own robot execution, perception, or motion — those are in `decentralised`.

| Path | Layer | Must not |
|---|---|---|
| `reasoning-layer/reasoning/planner.py`, `gemini.py` | ① | allocate robots or emit actions |
| `structured_mission_interface/` | ② | plan or allocate |
| `reasoning-layer/reasoning/orchestration.py`, `mission-orchestrator/` | ③ | trust LLM output without `validate_plan.py` |

## Rules
- Follow the shared rules in `docs/AGENT_WORKFLOW.md` §2. In short: stay in your layer; contract changes need an ADR + version bump; never claim planned work is done; report doc/code discrepancies instead of choosing one; don't weaken tests; no new frameworks without an ADR.
- Model output is untrusted input. Every new model call needs a schema and a deterministic validator.
- Keep the pipeline runnable offline (no API key) — `demo_pipeline.py` and all tests must pass without network.
- Never commit `.env` or keys. Don't raise API budgets or add retries without saying so in the PR.

## Checks (run before finishing)
```bash
uv sync --locked
uv run python -m unittest discover -s reasoning-layer/tests -t reasoning-layer
uv run python -m unittest discover -s tests
uv run python demo_pipeline.py
```
Expected today: 62 + 36 tests OK; demo prints `"status": "ready"`. If `uv` is not installed, install it (https://docs.astral.sh/uv/) rather than switching tools.
