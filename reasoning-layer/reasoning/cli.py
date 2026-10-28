"""JSON-file CLI. Only a ready result can create an orchestrator handoff."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from .contracts import ContractError, MissionRequest, generation_schema, parse_json, validate_context
from .gemini import DEFAULT_MODEL, GeminiProvider
from .planner import GenerationError, MissionPlanner, SYSTEM_PROMPT, build_prompt

ROOT = Path(__file__).resolve().parents[2]


def write_new_json(path: Path, payload: dict) -> None:
    """Publish a complete JSON file atomically, refusing to replace existing output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def add_generation_arguments(parser):
    """Shared provider configuration for the reasoning and connected CLIs."""
    parser.add_argument("--fleet", type=Path, default=ROOT / "mission-orchestrator/robots.json")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--model", help="Gemini model ID; otherwise GEMINI_MODEL or gemini-3.5-flash-lite")
    parser.add_argument("--timeout-ms", type=int, default=45000, help="Timeout per API call")
    parser.add_argument("--max-output-tokens", type=int, default=2048, help="Output token budget per call")
    parser.add_argument("--max-prompt-chars", type=int, default=30000,
                        help="Character budget for system, context and schema combined")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate candidate tasks for the Mission Orchestrator.")
    parser.add_argument("--input", type=Path, required=True, help="Mission request JSON file")
    parser.add_argument("--output", type=Path, help="New handoff file; defaults to JSON on stdout")
    add_generation_arguments(parser)
    parser.add_argument("--max-attempts", type=int, choices=(1, 2), default=1,
                        help="Total generation attempts, including format repair")
    parser.add_argument("--dry-run", action="store_true", help="Show context/schema without an API call")
    args = parser.parse_args(argv)
    try:
        if args.output and (args.output.exists() or args.output.is_symlink()):
            raise ContractError("Output already exists. Choose a new path to avoid a stale handoff.")
        request = MissionRequest.model_validate(parse_json(args.input.read_text(encoding="utf-8")))
        fleet = parse_json(args.fleet.read_text(encoding="utf-8"))
        if not isinstance(fleet, dict):
            raise ContractError("Fleet must be a JSON object.")
        validate_context(request, fleet)
        load_dotenv(args.env_file, override=False)
        model = args.model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        if args.dry_run:
            print(json.dumps({
                "model": model,
                "system_instruction": SYSTEM_PROMPT,
                "context": parse_json(build_prompt(request, fleet)),
                "output_schema": generation_schema(),
            }, indent=2, ensure_ascii=False, allow_nan=False))
            return 0
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        provider = GeminiProvider(key, model=model, timeout_ms=args.timeout_ms,
                                  max_output_tokens=args.max_output_tokens,
                                  max_prompt_chars=args.max_prompt_chars)
        outcome = MissionPlanner(provider, max_attempts=args.max_attempts).plan(request, fleet)
        for index, usage in enumerate(getattr(provider, "usage", []), start=1):
            counts = ", ".join(f"{kind}={count if count is not None else 'unknown'}"
                               for kind, count in usage.items())
            print(f"Gemini response {index} token usage: {counts}", file=sys.stderr)
        if outcome.handoff is None:
            print(json.dumps({
                "mission_id": request.mission_id,
                **outcome.decision.model_dump(exclude={"tasks"}),
            }, indent=2, ensure_ascii=False))
            print("No handoff produced: mission needs clarification or is infeasible.", file=sys.stderr)
            return 3
        if args.output:
            write_new_json(args.output, outcome.handoff)
            print(f"Candidate handoff written to {args.output}", file=sys.stderr)
        else:
            print(json.dumps(outcome.handoff, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except ValidationError as error:
        locations = [".".join(str(part) for part in item["loc"]) or "request"
                     for item in error.errors(include_input=False, include_url=False)]
        print("ERROR: Invalid request fields: " + ", ".join(locations), file=sys.stderr)
    except (OSError, ValueError, GenerationError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
    return 2
