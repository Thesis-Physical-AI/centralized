#!/usr/bin/env python3
"""Generate a validated dispatch plan without sending robot commands."""

from pathlib import Path
import sys

# Both source modules live in this checkout; no editable install is required.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reasoning.pipeline_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
