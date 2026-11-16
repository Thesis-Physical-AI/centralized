"""Replanning proposal with optional validated orchestration; activation is separate."""
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from .cli import add_generation_arguments, write_new_json
from .contracts import MissionRequest, generation_schema, parse_json
from .gemini import DEFAULT_MODEL, GeminiProvider
from .planner import GenerationError, MissionPlanner, SYSTEM_PROMPT, build_prompt
from .replanning import prepare_replanning


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Original mission request")
    parser.add_argument("--dispatch", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--plan-revision", required=True, type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--history", type=Path, help="Validated prior revision history and stable objectives")
    parser.add_argument("--clarification", type=Path, help="JSON object with the user's clarification text")
    parser.add_argument("--dispatch-output", type=Path, help="Orchestrate a ready proposal into a new validated dispatch")
    add_generation_arguments(parser)
    args = parser.parse_args(argv)
    provider = None
    try:
        if args.output and (args.output.exists() or args.output.is_symlink()):
            raise ValueError("Output exists; choose a new proposal path")
        read = lambda path: parse_json(path.read_text(encoding="utf-8"))
        request = MissionRequest.model_validate(read(args.input))
        fleet, dispatch, report = read(args.fleet), read(args.dispatch), read(args.report)
        history = read(args.history) if args.history else None
        clarification = read(args.clarification).get("text", "") if args.clarification else ""
        if args.dispatch_output and args.output and args.dispatch_output.resolve() == args.output.resolve():
            raise ValueError("Proposal and dispatch outputs must differ")
        if args.dispatch_output and args.dispatch_output.exists():
            raise ValueError("Dispatch output already exists")
        updated, context = prepare_replanning(request, fleet, dispatch=dispatch, report=report,
                                             plan_revision=args.plan_revision, history=history, clarification=clarification)
        if args.dry_run:
            payload = {"system_instruction": SYSTEM_PROMPT,
                       "context": parse_json(build_prompt(updated, fleet, execution_context=context)),
                       "output_schema": generation_schema(replanning=True), "max_generation_calls": 2 if args.dispatch_output else 1,
                       "requires_review": True, "sends_robot_commands": False}
        else:
            load_dotenv(args.env_file, override=False)
            provider = GeminiProvider(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""),
                                      model=args.model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL),
                                      timeout_ms=args.timeout_ms, max_output_tokens=args.max_output_tokens,
                                      max_prompt_chars=args.max_prompt_chars)
            payload = MissionPlanner(provider).replan(request, fleet, dispatch=dispatch, report=report,
                                                      plan_revision=args.plan_revision, history=history, clarification=clarification)
            if args.dispatch_output and payload["handoff"] is not None:
                from .orchestration import MissionOrchestrator
                from .contracts import active_fleet
                outcome = MissionOrchestrator(provider).orchestrate(payload["handoff"], active_fleet(fleet, updated.world))
                payload["orchestration"] = {"status": outcome.status, "errors": outcome.errors, "warnings": outcome.warnings}
                if outcome.plan is not None:
                    write_new_json(args.dispatch_output, outcome.plan)
        if args.output:
            write_new_json(args.output, payload)
        else:
            print(json.dumps(payload, indent=2, allow_nan=False))
        return 0 if args.dry_run or payload["status"] == "ready" else 3
    except (OSError, ValueError, KeyError, TypeError, GenerationError, ValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        for index, usage in enumerate(getattr(provider, "usage", []), 1):
            print(f"Gemini response {index} token usage: {usage}", file=sys.stderr)
