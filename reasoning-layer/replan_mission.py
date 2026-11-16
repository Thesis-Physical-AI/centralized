"""Run replanning and optional orchestration from the repository root."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reasoning.replan_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
