from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from mission_lifecycle import MissionLifecycle
from structured_mission_interface.interface import DEFAULT_FLEET, load_json
from reasoning.contracts import MissionRequest
from reasoning.replanning import prepare_replanning


def fixture(folder, *, max_revisions=3):
    world = {"entities": {"room": {"kind": "room"}, "table": {"kind": "surface", "in": "room"},
                          "crate": {"kind": "container", "in": "room"},
                          "apple": {"kind": "object", "in": "table"}, "toy": {"kind": "object", "in": "room"}},
             "robots": {"puppypi_1": {"at": "room"}}}
    request = {"mission_id": "M-LIFE", "natural_language": "Find apple and toy, then inspect table", "world": world}
    def step(sid, tid, action, args, depends=None):
        return {"id": sid, "task_id": tid, "robot": "puppypi_1", "action": action,
                "args": args, "depends_on": depends or []}
    plan = {"mission_id": "M-LIFE", "world": deepcopy(world), "unassigned": [], "allocations": [], "steps": [
        step("S1", "T1", "search", {"object": "apple", "area": "room"}),
        step("S2", "T2", "search", {"object": "toy", "area": "room"}),
        step("S3", "T3", "inspect", {"target": "table"}, ["S2"])]}
    life = MissionLifecycle(folder, request, plan, load_json(DEFAULT_FLEET), max_revisions=max_revisions)
    report = make_report(plan, 0, ["SUCCEEDED", "BLOCKED", "NOT_DISPATCHED"])
    return life, report, step


def checkpoint(revision=0, token="idle-0"):
    return {"mission_id": "M-LIFE", "session_id": "physical-session", "active_revision": revision,
            "phase": "IDLE", "quiescence_token": token,
            "checkpoint": {"pose": [.2, .1, .5], "sim_time": 12 + revision, "safety": ""}}


def pause_ack():
    return {"accepted": True, "command_id": "pause-0", **checkpoint()}


def make_report(plan, revision, statuses):
    results = []
    for step, status in zip(plan["steps"], statuses):
        if status == "NOT_DISPATCHED":
            result = {"step_id": step["id"], "robot_id": step["robot"], "status": status}
        else:
            result = {"mission_id": "M-LIFE", "plan_revision": revision, "step_id": step["id"],
                      "robot_id": step["robot"], "attempt": 1, "status": status,
                      "reason_code": "" if status == "SUCCEEDED" else "OBJECT_NOT_FOUND", "observations": []}
        results.append(result)
    status = "SUCCEEDED" if all(s == "SUCCEEDED" for s in statuses) else "HALTED"
    return {"mission_id": "M-LIFE", "execution": {"mission_id": "M-LIFE", "plan_revision": revision,
            "status": status, "steps": results}, "checkpoint": checkpoint(revision, f"idle-{revision}")}


def candidate(life, step, *, unchanged=False):
    world = life.current["world"]
    proposal = {"mission_id": "M-LIFE", "source_plan_revision": 0, "proposed_plan_revision": 1,
                "status": "ready", "objective_coverage": {"R1_T2": ["T2"], "R1_T3": ["T3"]},
                "handoff": {"mission_id": "M-LIFE", "world": deepcopy(world), "tasks": [
                    {"task_id": "R1_T2", "description": "Search the known crate for toy", "constraints": {}},
                    {"task_id": "R1_T3", "description": "Inspect table", "depends_on": ["R1_T2"], "constraints": {}}]}}
    steps = [step("N1", "R1_T2", "search", {"object": "toy", "area": "room" if unchanged else "crate"}),
             step("N2", "R1_T3", "inspect", {"target": "table"}, ["N1"])]
    dispatch = {"mission_id": "M-LIFE", "world": deepcopy(world), "steps": steps, "unassigned": [],
                "allocations": [{"task_id": t, "robots": ["puppypi_1"], "rationale": "Test recovery"}
                                for t in ("R1_T2", "R1_T3")]}
    return proposal, dispatch


class LifecycleTests(unittest.TestCase):
    def prepare(self, folder, **kwargs):
        life, report, step = fixture(folder, **kwargs)
        life.record_report(report)
        life.reconcile_pose({"room": {"size": [4, 4]}, "robot": {"id": "puppypi_1"},
                             "entities": {"crate": {"approach": [1, 0, 0]}, "table": {"approach": [0, 1, 0]}}})
        return life, report, step

    def test_activation_preserves_history_and_finishes_remaining_objectives(self):
        with tempfile.TemporaryDirectory() as folder:
            life, report, step = self.prepare(folder)
            self.assertEqual(life.data["completed_objective_ids"], ["T1"])
            proposal, plan = candidate(life, step)
            self.assertEqual(life.offer(proposal, plan), [])
            command = life.begin_activation(checkpoint(), pause_ack=pause_ack())
            self.assertEqual(life.data["active_revision"], 0)  # no switch before ack
            ack = {"accepted": True, "command_id": command["command_id"], "mission_id": "M-LIFE",
                   "session_id": "physical-session", "active_revision": 1, "phase": "RUNNING"}
            life.commit_activation(ack)
            self.assertEqual(life.data["active_revision"], 1)
            world = deepcopy(life.current["world"])
            self.assertIsNone(life.record_report(report))  # late old revision is archived
            self.assertEqual(life.current["world"], world)
            self.assertEqual(len(list((Path(folder) / "late").glob("*.json"))), 1)
            life.record_report(make_report(life.current["dispatch"], 1, ["SUCCEEDED", "SUCCEEDED"]))
            self.assertEqual(life.data["status"], "SUCCEEDED")
            self.assertEqual(life.data["completed_objective_ids"], ["T1", "T2", "T3"])
            self.assertEqual(len(life.data["revisions"]), 2)
            self.assertEqual(json.loads((Path(folder) / "lifecycle.json").read_text())["active_revision"], 1)

    def test_no_progress_budget_and_review_gates(self):
        with tempfile.TemporaryDirectory() as folder:
            life, _, step = self.prepare(folder, max_revisions=0)
            proposal, plan = candidate(life, step, unchanged=True)
            reasons = life.offer(proposal, plan)
            self.assertTrue(any("unchanged" in r for r in reasons))
            self.assertTrue(any("limit" in r for r in reasons))
            with self.assertRaises(ValueError):
                life.begin_activation(checkpoint(), acknowledge_review=True)

    def test_missing_or_completed_objective_coverage_cannot_activate(self):
        for bad in ({"R1_T2": ["T2"], "R1_T3": ["T1"]}, {"R1_T2": ["T2"]}):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as folder:
                life, _, step = self.prepare(folder)
                proposal, plan = candidate(life, step)
                proposal["objective_coverage"] = bad
                with self.assertRaisesRegex(ValueError, "objective"):
                    life.offer(proposal, plan)
                self.assertIsNone(life.data["pending"])

    def test_stale_checkpoint_and_wrong_ack_are_not_activation(self):
        with tempfile.TemporaryDirectory() as folder:
            life, _, step = self.prepare(folder)
            life.offer(*candidate(life, step))
            with self.assertRaises(ValueError):
                life.begin_activation(checkpoint(token="changed"), pause_ack=pause_ack())
            command = life.begin_activation(checkpoint(), pause_ack=pause_ack())
            with self.assertRaises(ValueError):
                life.commit_activation({"accepted": True, "command_id": command["command_id"],
                                        "session_id": "other", "active_revision": 1, "phase": "RUNNING"})
            self.assertEqual(life.data["active_revision"], 0)
            self.assertEqual(life.data["status"], "ACTIVATING")

    def test_reset_simulator_and_conflicting_report_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            life, report, _ = self.prepare(folder)
            changed = deepcopy(report)
            changed["checkpoint"]["session_id"] = "reset"
            with self.assertRaises(ValueError):
                life.record_report(changed)
            outside = deepcopy(life.current["checkpoint"])
            outside["checkpoint"]["pose"] = [10, 0, 0]
            life.current["checkpoint"] = outside
            with self.assertRaisesRegex(ValueError, "outside"):
                life.reconcile_pose({"room": {"size": [4, 4]}, "robot": {"id": "puppypi_1"}})

    def test_recovery_cannot_relabel_an_easier_action_as_the_missing_goal(self):
        with tempfile.TemporaryDirectory() as folder:
            life, _, step = self.prepare(folder)
            proposal, plan = candidate(life, step)
            plan["steps"][0].update(action="inspect", args={"target": "crate"})
            with self.assertRaisesRegex(ValueError, "unfinished"):
                life.offer(proposal, plan)

    def test_new_area_without_a_different_view_is_not_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            life, _, step = self.prepare(folder)
            del life.current["reconciliation"]["waypoints"]["crate"]
            reasons = life.offer(*candidate(life, step))
            self.assertTrue(any("new reachable viewpoint" in reason for reason in reasons))

    def test_table_visited_for_apple_is_still_a_new_toy_search_view(self):
        with tempfile.TemporaryDirectory() as folder:
            life, _, step = fixture(folder)
            plan = life.current["dispatch"]
            plan["steps"].insert(0, step("S0", "T1", "movetowards", {"target": "table"}))
            plan["steps"][2]["args"]["area"] = "crate"
            source = make_report(plan, 0, ["SUCCEEDED", "SUCCEEDED", "BLOCKED", "NOT_DISPATCHED"])
            source["checkpoint"]["checkpoint"]["pose"] = [1, 0, 0]
            life.record_report(source)
            life.reconcile_pose({"room": {"size": [4, 4]}, "robot": {"id": "puppypi_1"},
                                 "entities": {"crate": {"approach": [1, 0, 0]}, "table": {"approach": [0, 1, 0]}}})
            proposal, replacement = candidate(life, step)
            replacement["steps"][0]["args"]["area"] = "table"
            self.assertEqual(life.offer(proposal, replacement), [])

    def test_second_failure_keeps_completed_objectives_in_the_next_reasoning_context(self):
        with tempfile.TemporaryDirectory() as folder:
            life, _, step = self.prepare(folder)
            life.offer(*candidate(life, step))
            command = life.begin_activation(checkpoint(), pause_ack=pause_ack())
            life.commit_activation({"accepted": True, "command_id": command["command_id"], "mission_id": "M-LIFE",
                                    "session_id": "physical-session", "active_revision": 1, "phase": "RUNNING"})
            next_report = make_report(life.current["dispatch"], 1, ["SUCCEEDED", "BLOCKED"])
            life.record_report(next_report)
            life.reconcile_pose({"room": {"size": [4, 4]}, "robot": {"id": "puppypi_1"},
                                 "entities": {"table": {"approach": [0, 1, 0]}, "crate": {"approach": [1, 0, 0]}}})
            updated, context = prepare_replanning(MissionRequest.model_validate(life.current["request"]), life.fleet,
                dispatch=life.current["dispatch"], report=next_report, plan_revision=1,
                history=life.history(), clarification="Please inspect the remaining target.")
            self.assertEqual(context["completed_objective_ids"], ["T1", "T2"])
            self.assertEqual([o["objective_id"] for o in context["remaining_objectives"]], ["T3"])
            self.assertEqual(context["proposed_plan_revision"], 2)
            self.assertEqual(len(context["previous_revisions"]), 1)
            self.assertEqual(updated.natural_language, life.data["original_request"]["natural_language"])

    def test_unresolved_perception_review_requires_explicit_operator_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            life, report, step = self.prepare(folder)
            life.current["feedback"]["review"].append({"event_id": "E1", "reason": "observation requires review"})
            reasons = life.offer(*candidate(life, step))
            self.assertTrue(any("operator" in r for r in reasons))
            self.assertFalse(life.activation_reasons(acknowledge_review=True))
