from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from structured_mission_interface.feedback import ingest_feedback
from structured_mission_interface.interface import validate_world_context, load_json, DEFAULT_FLEET


def inputs():
    world = {"entities": {"room": {"kind": "room"}, "table": {"kind": "surface", "in": "room"},
                          "apple": {"kind": "object", "in": "table"}},
             "robots": {"puppypi_1": {"at": "room"}}}
    steps = [{"id": "S1", "task_id": "T1", "robot": "puppypi_1", "action": "movetowards",
              "args": {"target": "table"}, "depends_on": []},
             {"id": "S2", "task_id": "T1", "robot": "puppypi_1", "action": "search",
              "args": {"object": "apple", "area": "room"}, "depends_on": []}]
    results = [{"mission_id": "M", "plan_revision": 2, "step_id": s["id"], "robot_id": s["robot"],
                "attempt": 1, "status": "SUCCEEDED", "reason_code": "", "observations": []} for s in steps]
    results[1]["observations"] = [{"robot_id": "puppypi_1", "kind": "entity_located", "subject": "apple",
                                   "value": {"in": "room"}, "confidence": 1.0, "source": "perception",
                                   "mission_id": "M", "step_id": "S2", "event_id": "E1", "stamp": "stamp"}]
    return ({"mission_id": "M", "world": world, "steps": steps, "unassigned": []},
            {"mission_id": "M", "plan_revision": 2, "status": "SUCCEEDED", "steps": results})


class FeedbackTests(unittest.TestCase):
    def test_success_updates_robot_and_preserves_precise_object_location(self):
        plan, report = inputs()
        original = deepcopy((plan, report))
        result = ingest_feedback(plan, report, plan_revision=2)
        self.assertEqual(result["world"]["robots"]["puppypi_1"]["at"], "table")
        self.assertEqual(result["world"]["entities"]["apple"]["in"], "table")
        self.assertEqual(result["tasks"], {"T1": "COMPLETED"})
        self.assertFalse(result["replanning_required"])
        self.assertEqual((plan, report), original)
        self.assertEqual(result, ingest_feedback(plan, report, plan_revision=2))
        self.assertTrue(validate_world_context(result["world"], load_json(DEFAULT_FLEET)).valid)

    def test_absence_is_evidence_and_failure_requires_replanning(self):
        plan, report = inputs()
        report["status"] = "HALTED"
        report["steps"][1].update(status="BLOCKED", reason_code="OBJECT_NOT_FOUND")
        report["steps"][1]["observations"][0]["kind"] = "entity_absent"
        plan["steps"].append({**plan["steps"][1], "id": "S3", "task_id": "T2"})
        report["steps"].append({"step_id": "S3", "robot_id": "puppypi_1", "status": "NOT_DISPATCHED"})
        result = ingest_feedback(plan, {"mission_id": "M", "execution": report}, plan_revision=2)
        self.assertEqual(result["world"]["entities"]["apple"]["in"], "table")
        self.assertEqual(result["tasks"], {"T1": "NEEDS_REVIEW", "T2": "NOT_STARTED"})
        self.assertTrue(result["replanning_required"])
        self.assertEqual(result["observations"][0]["kind"], "entity_absent")

    def test_new_unambiguous_location_can_be_filled(self):
        plan, report = inputs()
        del plan["world"]["entities"]["apple"]["in"]
        result = ingest_feedback(plan, report, plan_revision=2)
        self.assertEqual(result["world"]["entities"]["apple"]["in"], "room")

    def test_cross_robot_forward_dependencies_preserve_fleet(self):
        plan, report = inputs()
        plan["world"]["robots"]["drone_1"] = {"at": "room", "airborne": False}
        plan["steps"][0]["depends_on"] = ["SCOUT"]
        plan["steps"].append({"id": "SCOUT", "task_id": "T2", "robot": "drone_1", "action": "search",
                              "args": {"object": "apple", "area": "room"}})
        report["steps"].append({**report["steps"][0], "step_id": "SCOUT", "robot_id": "drone_1"})
        result = ingest_feedback(plan, report, plan_revision=2)
        self.assertEqual(set(result["world"]["robots"]), {"puppypi_1", "drone_1"})
        self.assertEqual(result["tasks"], {"T1": "COMPLETED", "T2": "COMPLETED"})

    def test_failed_movement_does_not_claim_arrival(self):
        plan, report = inputs()
        report["status"] = "HALTED"
        report["steps"][0].update(status="TIMED_OUT", reason_code="TIMEOUT")
        report["steps"][1] = {"step_id": "S2", "robot_id": "puppypi_1", "status": "NOT_DISPATCHED"}
        result = ingest_feedback(plan, report, plan_revision=2)
        self.assertEqual(result["world"]["robots"]["puppypi_1"]["at"], "room")
        self.assertTrue(any("partial physical effects" in r["reason"] for r in result["review"]))

    def test_conflict_and_vlm_evidence_are_held_for_review(self):
        for source in ["vlm", "perception"]:
            with self.subTest(source=source):
                plan, report = inputs()
                plan["world"]["entities"]["other"] = {"kind": "room"}
                plan["world"]["entities"]["apple"]["in"] = "other"
                report["steps"][1]["observations"][0]["source"] = source
                result = ingest_feedback(plan, report, plan_revision=2)
                self.assertEqual(result["world"]["entities"]["apple"]["in"], "other")
                self.assertTrue(result["review"])

    def test_rejects_invalid_feedback_atomically(self):
        changes = [
            lambda p, r: r.update(mission_id="wrong"),
            lambda p, r: r.update(plan_revision=1),
            lambda p, r: r["steps"][0].update(plan_revision=1),
            lambda p, r: r["steps"][0].update(robot_id="drone_1"),
            lambda p, r: r["steps"][0].update(attempt=2),
            lambda p, r: r["steps"][0].update(status="UNKNOWN"),
            lambda p, r: r["steps"][0].update(reason_code="TIMEOUT"),
            lambda p, r: r["steps"].append(deepcopy(r["steps"][0])),
            lambda p, r: r["steps"].pop(),
            lambda p, r: r["steps"][1]["observations"][0].update(subject="unknown"),
            lambda p, r: r["steps"][1]["observations"][0].update(mission_id="wrong"),
            lambda p, r: r["steps"][1]["observations"][0].update(confidence=float("nan")),
            lambda p, r: r["steps"][1]["observations"].append(deepcopy(r["steps"][1]["observations"][0])),
            lambda p, r: p["steps"][0].update(depends_on=["S2"]),
            lambda p, r: r.update(status="PENDING"),
            lambda p, r: r["steps"].__setitem__(0, {"step_id": "S1", "robot_id": "puppypi_1",
                                                   "status": "NOT_DISPATCHED"}),
        ]
        for change in changes:
            with self.subTest(change=change):
                plan, report = inputs()
                change(plan, report)
                original = deepcopy(plan)
                with self.assertRaises(ValueError):
                    ingest_feedback(plan, report, plan_revision=2)
                self.assertEqual(plan, original)

    def test_cli_writes_snapshot_and_refuses_overwrite(self):
        plan, report = inputs()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in [("dispatch", plan), ("report", report)]:
                (root / name).write_text(json.dumps(value))
            command = [sys.executable, "-m", "structured_mission_interface.feedback", "--dispatch",
                       str(root / "dispatch"), "--report", str(root / "report"), "--plan-revision", "2",
                       "--output", str(root / "snapshot")]
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            before = (root / "snapshot").read_text()
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertEqual((root / "snapshot").read_text(), before)
