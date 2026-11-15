from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from reasoning.contracts import ContractError
from reasoning.planner import GenerationError, MissionPlanner
from reasoning.replan_cli import main
from reasoning.replanning import prepare_replanning

from .test_planner import FakeProvider, fixtures


def inputs():
    request, fleet, _, _ = fixtures()
    steps = [
        {"id": "S1", "task_id": "T1", "robot": "drone_1", "action": "search",
         "args": {"object": "apple", "area": "kitchen"}},
        {"id": "S2", "task_id": "T2", "robot": "puppypi_1", "action": "movetowards",
         "args": {"target": "counter"}, "depends_on": ["S1"]},
        {"id": "S3", "task_id": "T2", "robot": "puppypi_1", "action": "search",
         "args": {"object": "juice_bottle", "area": "kitchen"}},
    ]
    report = {"mission_id": request.mission_id, "plan_revision": 0, "status": "HALTED", "steps": [
        {"mission_id": request.mission_id, "plan_revision": 0, "step_id": s["id"], "robot_id": s["robot"],
         "attempt": 1, "status": "SUCCEEDED", "reason_code": "", "observations": []} for s in steps[:2]]}
    report["steps"][1].update(status="BLOCKED", reason_code="PATH_BLOCKED", reason="route obstructed")
    report["steps"].append({"step_id": "S3", "robot_id": "puppypi_1", "status": "NOT_DISPATCHED"})
    return request, fleet, {"mission_id": request.mission_id, "world": deepcopy(request.world),
                            "steps": steps, "unassigned": []}, report


READY = {"status": "ready", "tasks": [{"task_id": "R1_T1", "description": "Recover delivery of apple and juice",
                                        "depends_on": [], "constraints": {}}], "questions": [], "reasons": []}


class ReplanningTests(unittest.TestCase):
    def test_context_reaches_provider_and_preserves_completed_history(self):
        request, fleet, dispatch, report = inputs()
        original = deepcopy((dispatch, report, request.world))
        provider = FakeProvider(READY)
        proposal = MissionPlanner(provider).replan(request, fleet, dispatch=dispatch, report=report, plan_revision=0)
        context = json.loads(provider.calls[0]["prompt"])
        self.assertEqual(context["natural_language"], request.natural_language)
        self.assertEqual(context["execution_feedback"]["completed_task_ids"], ["T1"])
        self.assertEqual(context["execution_feedback"]["step_results"]["S2"]["reason_code"], "PATH_BLOCKED")
        self.assertTrue(context["execution_feedback"]["review"])
        self.assertEqual(proposal["proposed_plan_revision"], 1)
        self.assertEqual(proposal["handoff"]["world"], context["world"])
        self.assertTrue(proposal["requires_review"])
        self.assertFalse(proposal["sends_robot_commands"])
        self.assertEqual((dispatch, report, request.world), original)
        self.assertEqual(len(provider.calls), 1)

    def test_invalid_feedback_stops_before_provider(self):
        for alteration in ["revision", "world", "mission"]:
            request, fleet, dispatch, report = inputs()
            if alteration == "revision":
                report["plan_revision"] = 3
            elif alteration == "world":
                dispatch["world"]["robots"]["puppypi_1"]["at"] = "counter"
            else:
                dispatch["mission_id"] = "other"
            provider = FakeProvider(READY)
            with self.assertRaises(ValueError):
                MissionPlanner(provider).replan(request, fleet, dispatch=dispatch, report=report, plan_revision=0)
            self.assertFalse(provider.calls)

    def test_reused_task_ids_rejected_and_repair_keeps_feedback(self):
        request, fleet, dispatch, report = inputs()
        bad = deepcopy(READY)
        bad["tasks"][0]["task_id"] = "T1"
        with self.assertRaises(GenerationError):
            MissionPlanner(FakeProvider(bad)).replan(request, fleet, dispatch=dispatch, report=report, plan_revision=0)
        provider = FakeProvider(bad, READY)
        MissionPlanner(provider, max_attempts=2).replan(request, fleet, dispatch=dispatch, report=report, plan_revision=0)
        repair = json.loads(provider.calls[1]["prompt"])
        self.assertIn("execution_feedback", repair["original_request"])

    def test_clarification_is_a_proposal_without_handoff(self):
        request, fleet, dispatch, report = inputs()
        provider = FakeProvider({"status": "needs_clarification", "tasks": [],
                                 "questions": ["Where is the robot after the blocked movement?"], "reasons": []})
        proposal = MissionPlanner(provider).replan(request, fleet, dispatch=dispatch, report=report, plan_revision=0)
        self.assertIsNone(proposal["handoff"])
        self.assertEqual(proposal["status"], "needs_clarification")

    def test_completed_mission_does_not_trigger_replanning(self):
        request, fleet, dispatch, report = inputs()
        report["steps"][1].update(status="SUCCEEDED", reason_code="")
        dispatch["steps"].pop()
        report["steps"].pop()
        report["status"] = "SUCCEEDED"
        with self.assertRaises(ContractError):
            prepare_replanning(request, fleet, dispatch=dispatch, report=report, plan_revision=0)

    def test_cli_dry_run_makes_no_api_call(self):
        request, fleet, dispatch, report = inputs()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, data in [("input", request.model_dump()), ("fleet", fleet),
                               ("dispatch", dispatch), ("report", report)]:
                (root / name).write_text(json.dumps(data))
            args = ["--input", str(root / "input"), "--fleet", str(root / "fleet"),
                    "--dispatch", str(root / "dispatch"), "--report", str(root / "report"),
                    "--plan-revision", "0", "--dry-run", "--output", str(root / "preview")]
            with patch("reasoning.replan_cli.GeminiProvider") as provider, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 0)
                provider.assert_not_called()
                self.assertEqual(main(args), 2)  # refuses overwrite
            preview = json.loads((root / "preview").read_text())
            self.assertEqual(preview["context"]["execution_feedback"]["completed_task_ids"], ["T1"])
            entry = Path(__file__).resolve().parents[1] / "replan_mission.py"
            result = subprocess.run([sys.executable, str(entry), *args[:-2]], cwd=directory,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["context"], preview["context"])
