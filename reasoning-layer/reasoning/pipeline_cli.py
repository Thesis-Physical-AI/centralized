"""Run both planning agents with validation gates and bounded API usage."""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from .cli import add_generation_arguments, write_new_json
from .contracts import ContractError, MissionRequest, parse_json, validate_context
from .gemini import DEFAULT_MODEL, GeminiProvider
from .interface import validate_handoff
from .orchestration import MissionOrchestrator
from .pipeline import MissionPipeline
from .planner import GenerationError, MissionPlanner


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reason about a mission and produce a validated dispatch plan.")
    parser.add_argument("--input", type=Path, required=True, help="Original mission request JSON")
    parser.add_argument("--output", type=Path, help="New validated dispatch JSON; otherwise stdout")
    parser.add_argument("--handoff", type=Path, help="Reuse a saved candidate and skip the reasoning API call")
    parser.add_argument("--handoff-output", type=Path, help="Save the interface-accepted candidate before orchestration")
    parser.add_argument("--dry-run", action="store_true", help="Check input and display API limits; no model calls")
    add_generation_arguments(parser)
    args = parser.parse_args(argv)
    provider = None
    try:
        paths = [path for path in (args.output, args.handoff_output) if path is not None]
        if len({path.resolve() for path in paths}) != len(paths):
            raise ContractError("Dispatch and handoff outputs must use different paths.")
        if any(path.exists() or path.is_symlink() for path in paths):
            raise ContractError("Output already exists. Choose a new path to avoid a stale plan.")
        request = MissionRequest.model_validate(parse_json(args.input.read_text(encoding="utf-8")))
        fleet = parse_json(args.fleet.read_text(encoding="utf-8"))
        if not isinstance(fleet, dict):
            raise ContractError("Fleet must be a JSON object.")
        validate_context(request, fleet)
        handoff = None
        if args.handoff:
            handoff = validate_handoff(parse_json(args.handoff.read_text(encoding="utf-8")), request, fleet)
        load_dotenv(args.env_file, override=False)
        model = args.model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        if args.dry_run:
            print(json.dumps({
                "mission_id": request.mission_id,
                "model": model,
                "reasoning_reused": handoff is not None,
                "max_generation_calls": 1 if handoff is not None else 2,
                "max_output_tokens_per_call": args.max_output_tokens,
                "max_prompt_chars_per_call": args.max_prompt_chars,
                "http_retries": 0,
                "sends_robot_commands": False,
            }, indent=2))
            return 0
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        provider = GeminiProvider(key, model=model, timeout_ms=args.timeout_ms,
                                  max_output_tokens=args.max_output_tokens,
                                  max_prompt_chars=args.max_prompt_chars)
        pipeline = MissionPipeline(MissionPlanner(provider, max_attempts=1), MissionOrchestrator(provider))

        def save_handoff(accepted):
            if args.handoff_output:
                write_new_json(args.handoff_output, accepted)
                print(f"Interface-accepted candidate written to {args.handoff_output}", file=sys.stderr)

        result = pipeline.run(request, fleet, handoff=handoff, on_handoff=save_handoff)
        if result.dispatch is None:
            print(json.dumps(result.report, indent=2, ensure_ascii=False))
            print("No dispatch plan published.", file=sys.stderr)
            return 2 if result.status == "invalid_dispatch" else 3
        if args.output:
            write_new_json(args.output, result.dispatch)
            print(f"Validated dispatch plan written to {args.output}", file=sys.stderr)
        else:
            print(json.dumps(result.dispatch, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except ValidationError as error:
        locations = [".".join(str(part) for part in item["loc"]) or "request/handoff"
                     for item in error.errors(include_input=False, include_url=False)]
        print("ERROR: Invalid fields: " + ", ".join(locations), file=sys.stderr)
    except (OSError, ValueError, GenerationError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
    finally:
        for index, usage in enumerate(getattr(provider, "usage", []), start=1):
            counts = ", ".join(f"{kind}={count if count is not None else 'unknown'}"
                               for kind, count in usage.items())
            print(f"Gemini response {index} token usage: {counts}", file=sys.stderr)
    return 2
