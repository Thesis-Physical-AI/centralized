import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reasoning.pipeline_cli import main
from tests.test_pipeline import dispatch_fixture
from tests.test_planner import FakeProvider, ROOT, fixtures


class PipelineCliTests(unittest.TestCase):
    def setUp(self):
        self.request, self.fleet, self.handoff, self.decision = fixtures()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.input = ROOT / "reasoning-layer/examples/request.json"

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--input", str(self.input), "--env-file", str(self.directory / "absent.env"), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    @patch("reasoning.pipeline_cli.GeminiProvider")
    def test_two_stage_run_writes_accepted_candidate_and_final_dispatch(self, provider):
        fake = FakeProvider(self.decision, dispatch_fixture())
        provider.return_value = fake
        handoff = self.directory / "candidate.json"
        output = self.directory / "dispatch.json"
        code, stdout, stderr = self.invoke("--output", str(output), "--handoff-output", str(handoff))
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(json.loads(handoff.read_text()), self.handoff)
        self.assertEqual(json.loads(output.read_text())["mission_id"], self.request.mission_id)
        self.assertEqual(stdout, "")

    @patch("reasoning.pipeline_cli.GeminiProvider")
    def test_rejected_dispatch_never_creates_final_output(self, provider):
        provider.return_value = FakeProvider(self.decision, {"allocations": [], "steps": [], "unassigned": []})
        output = self.directory / "dispatch.json"
        code, stdout, stderr = self.invoke("--output", str(output))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["status"], "invalid_dispatch")
        self.assertFalse(output.exists())

    @patch("reasoning.pipeline_cli.GeminiProvider")
    def test_cached_handoff_skips_reasoning(self, provider):
        fake = FakeProvider(dispatch_fixture())
        provider.return_value = fake
        handoff = self.directory / "candidate.json"
        handoff.write_text(json.dumps(self.handoff))
        code, stdout, stderr = self.invoke("--handoff", str(handoff))
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(json.loads(stdout)["steps"]), 19)

    @patch("reasoning.pipeline_cli.GeminiProvider")
    def test_stale_cached_handoff_is_rejected_before_provider(self, provider):
        self.handoff["world"]["open"] = ["kitchen_door"]
        handoff = self.directory / "stale.json"
        handoff.write_text(json.dumps(self.handoff))
        code, stdout, stderr = self.invoke("--handoff", str(handoff))
        self.assertEqual(code, 2)
        self.assertIn("snapshot", stderr)
        provider.assert_not_called()

    @patch("reasoning.pipeline_cli.GeminiProvider")
    def test_existing_output_is_rejected_before_any_call(self, provider):
        output = self.directory / "dispatch.json"
        output.write_text("existing")
        code, stdout, stderr = self.invoke("--output", str(output))
        self.assertEqual(code, 2)
        self.assertEqual(output.read_text(), "existing")
        provider.assert_not_called()

    @patch("reasoning.pipeline_cli.GeminiProvider")
    def test_dry_run_displays_two_call_budget_without_provider(self, provider):
        code, stdout, stderr = self.invoke("--dry-run")
        self.assertEqual(code, 0)
        report = json.loads(stdout)
        self.assertEqual(report["max_generation_calls"], 2)
        self.assertEqual(report["max_output_tokens_per_call"], 2048)
        self.assertFalse(report["sends_robot_commands"])
        provider.assert_not_called()

    @patch("reasoning.pipeline_cli.GeminiProvider")
    def test_cannot_use_same_path_for_both_outputs(self, provider):
        path = str(self.directory / "plan.json")
        code, stdout, stderr = self.invoke("--output", path, "--handoff-output", path)
        self.assertEqual(code, 2)
        provider.assert_not_called()
