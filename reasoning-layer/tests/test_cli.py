import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reasoning.cli import main
from tests.test_planner import FakeProvider, ROOT, fixtures


class CliTests(unittest.TestCase):
    def setUp(self):
        self.request, self.fleet, self.handoff, self.decision = fixtures()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.input = ROOT / "reasoning-layer/examples/request.json"
        self.env = self.directory / "absent.env"

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--input", str(self.input), "--env-file", str(self.env), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    @patch("reasoning.cli.GeminiProvider")
    def test_dry_run_needs_no_key_and_makes_no_call(self, provider):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "do-not-print-this"}):
            code, stdout, stderr = self.invoke("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["context"]["world"], self.request.world)
        self.assertNotIn("do-not-print-this", stdout + stderr)
        provider.assert_not_called()

    @patch("reasoning.cli.GeminiProvider")
    def test_ready_writes_only_contract_and_refuses_overwrite(self, provider):
        provider.return_value = FakeProvider(self.decision)
        output = self.directory / "nested/handoff.json"
        code, stdout, stderr = self.invoke("--output", str(output))
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(output.read_text()), self.handoff)
        provider.reset_mock()
        code, stdout, stderr = self.invoke("--output", str(output))
        self.assertEqual(code, 2)
        self.assertIn("already exists", stderr)
        provider.assert_not_called()

    @patch("reasoning.cli.GeminiProvider")
    def test_non_ready_does_not_create_handoff_file(self, provider):
        provider.return_value = FakeProvider({"status": "needs_clarification", "questions": ["Which room?"]})
        output = self.directory / "handoff.json"
        code, stdout, stderr = self.invoke("--output", str(output))
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(stdout)["status"], "needs_clarification")
        self.assertNotIn("tasks", json.loads(stdout))
        self.assertFalse(output.exists())

    def test_missing_key_is_an_actionable_error(self):
        with patch.dict(os.environ, {}, clear=True):
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("GEMINI_API_KEY", stderr)

    @patch("reasoning.cli.GeminiProvider")
    def test_malformed_input_does_not_call_provider(self, provider):
        self.input = self.directory / "invalid.json"
        self.input.write_text('{"mission_id": "M1"}')
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("natural_language", stderr)
        provider.assert_not_called()
