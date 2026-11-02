"""Run the complete offline entry point outside the repository working directory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DemoPipelineTests(unittest.TestCase):
    def test_offline_end_to_end_without_api_credentials(self):
        env = dict(os.environ)
        for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            env.pop(key, None)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "demo_pipeline.py")],
                                    cwd=directory, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["layers"], ["reasoning", "structured_mission_interface",
                                            "orchestrator", "dispatch_validation"])
        self.assertEqual(report["fixture_responses_consumed"], 2)
        self.assertEqual(report["api_calls"], 0)
        self.assertFalse(report["sends_robot_commands"])
        self.assertEqual(report["step_count"], 19)
