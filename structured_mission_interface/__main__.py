"""Inspect a candidate with the same validation used by the connected pipeline."""
import argparse
import json
from pathlib import Path

from .interface import DEFAULT_FLEET, Issue, ValidationResult, load_json, validate_mission
from .schema import MISSION_SCHEMA


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a mission candidate before orchestration.")
    parser.add_argument("mission", type=Path, nargs="?")
    parser.add_argument("--fleet", type=Path, default=DEFAULT_FLEET)
    parser.add_argument("--output", type=Path, help="save accepted mission input only")
    parser.add_argument("--print-schema", action="store_true")
    args = parser.parse_args(argv)
    if args.print_schema:
        print(json.dumps(MISSION_SCHEMA, indent=2))
        return 0
    if args.mission is None:
        parser.error("mission is required unless --print-schema is used")
    if args.output and args.output.resolve() in {args.mission.resolve(), args.fleet.resolve()}:
        parser.error("output must not overwrite input or fleet")
    try:
        result = validate_mission(load_json(args.mission), load_json(args.fleet))
        if result.valid and args.output:
            args.output.write_text(json.dumps(result.mission, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        result = ValidationResult((Issue("/", "input_error", str(exc)),))
    print(json.dumps(result.to_dict(), indent=2, allow_nan=False))
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
