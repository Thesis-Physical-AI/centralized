import io
import json
from pathlib import Path
import sys
import tempfile
from threading import Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mission_dashboard import Console, make_handler


class FakeConsole(Console):
    def __init__(self, output):
        super().__init__(output)
        self.commands = []

    def ensure_vlm(self, folder):
        self.commands.append("vlm")

    def command(self, args, cwd, folder, name, timeout):
        self.commands.append(name)
        request = json.loads((folder / "request.json").read_text())
        mid = request["mission_id"]
        if name == "planning":
            plan = {"mission_id": mid, "world": request["world"], "unassigned": [], "steps": [
                {"id": "S1", "task_id": "T1", "robot": "puppypi_1", "action": "search",
                 "args": {"object": "apple", "area": "living_room"}},
                {"id": "S2", "task_id": "T2", "robot": "puppypi_1", "action": "search",
                 "args": {"object": "toy", "area": "living_room"}}]}
            (folder / "dispatch.json").write_text(json.dumps(plan))
        elif name == "simulation":
            target = folder / "simulation"
            target.mkdir()
            steps = [{"mission_id": mid, "plan_revision": 0, "step_id": "S1", "robot_id": "puppypi_1",
                      "attempt": 1, "status": "SUCCEEDED", "reason_code": "", "observations": []},
                     {"mission_id": mid, "plan_revision": 0, "step_id": "S2", "robot_id": "puppypi_1",
                      "attempt": 1, "status": "BLOCKED", "reason_code": "OBJECT_NOT_FOUND", "observations": []}]
            (target / "results.json").write_text(json.dumps({"mission_id": mid, "execution": {
                "mission_id": mid, "plan_revision": 0, "status": "HALTED", "steps": steps}}))
            return 1  # Isaac wrapper returns nonzero for an unsuccessful mission.
        elif name == "replanning":
            proposal = {"status": "needs_clarification", "requires_review": True, "handoff": None,
                        "decision": {"status": "needs_clarification", "tasks": [],
                                     "questions": ["Where else should we search?"], "reasons": []}}
            (folder / "replan.json").write_text(json.dumps(proposal))
            return 3
        else:
            return super().command([sys.executable, *args[1:]], cwd, folder, name, timeout)
        return 0


class DashboardTests(unittest.TestCase):
    def test_full_coordinator_ingests_feedback_and_displays_clarification(self):
        with tempfile.TemporaryDirectory() as folder:
            console = FakeConsole(folder)
            with patch("mission_dashboard.Thread"):
                identity = console.submit("Find the apple and toy.", "vlm")
            console.run(identity)
            state = console.state()
            self.assertEqual(state["status"], "needs_attention")
            self.assertEqual(state["results"]["feedback"]["tasks"], {"T1": "COMPLETED", "T2": "NEEDS_REVIEW"})
            self.assertIsNone(state["results"]["replan"]["handoff"])
            self.assertEqual(state["completed"], ["planning", "perception", "execution", "feedback", "replanning"])
            self.assertIn("vlm", console.commands)
            restored = Console(folder).state()
            self.assertEqual(restored["id"], identity)
            self.assertEqual(restored["results"]["feedback"]["status"], "HALTED")

    def test_rejects_double_submission_and_invalid_missions(self):
        with tempfile.TemporaryDirectory() as folder:
            console = Console(folder)
            for text, mode in [("", "vlm"), ("x" * 5001, "vlm"), ("Go", "shell")]:
                with self.assertRaises(ValueError):
                    console.submit(text, mode)
            with patch("mission_dashboard.Thread"):
                console.submit("Find apple", "ground_truth")
                with self.assertRaises(ValueError):
                    console.submit("Find toy", "vlm")

    def test_stop_before_work_does_not_launch_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            console = FakeConsole(folder)
            with patch("mission_dashboard.Thread"):
                identity = console.submit("Find apple", "vlm")
            console.stop()
            console.run(identity)
            self.assertEqual(console.state()["status"], "stopped")
            self.assertNotIn("simulation", console.commands)

    def test_stop_terminates_owned_subprocess(self):
        with tempfile.TemporaryDirectory() as folder:
            console = Console(folder)
            marker = Path(folder) / "started"
            errors = []
            def run():
                try:
                    console.command([sys.executable, "-c",
                                     "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)",
                                     marker], folder, Path(folder), "owned-child", 10)
                except InterruptedError:
                    errors.append("stopped")
            worker = Thread(target=run)
            worker.start()
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists())
            console.stop()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ["stopped"])

    def test_initial_clarification_does_not_start_simulator(self):
        for code in (2, 3):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                console = Console(folder)
                def clarification(args, cwd, output, name, timeout):
                    self.assertEqual(name, "planning")
                    if code == 3:
                        (output / "planning.log").write_text(json.dumps({"status": "needs_clarification", "questions": ["Which room?"]}))
                    else:
                        (output / "planning-errors.log").write_text("ERROR: Model output failed the task contract.\n")
                    return code
                with patch("mission_dashboard.Thread"), patch.object(console, "command", clarification):
                    identity = console.submit("Find an object", "vlm")
                    console.run(identity)
                state = console.state()
                if code == 3:
                    self.assertEqual(state["results"]["decision"]["status"], "needs_clarification")
                else:
                    self.assertEqual(state["status"], "error")
                    self.assertIn("Model output failed the task contract", state["message"])

    def test_http_rejects_cross_origin_and_serves_no_arbitrary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            console = Console(folder)
            handler = object.__new__(make_handler(console))
            handler.server = SimpleNamespace(server_port=8787)
            captured = []
            handler.respond = lambda value, code=200, **kwargs: captured.append((code, value))
            handler.headers = {"Host": "127.0.0.1:8787", "Origin": "https://other.example", "Content-Length": "2"}
            handler.path = "/api/missions"
            handler.rfile = io.BytesIO(b"{}")
            handler.do_POST()
            self.assertEqual(captured[-1][0], 403)
            handler.path = "/.env"
            handler.do_GET()
            self.assertEqual(captured[-1][0], 404)
            handler.headers["Host"] = "other.example:8787"
            handler.path = "/api/state"
            handler.do_GET()
            self.assertEqual(captured[-1][0], 403)
