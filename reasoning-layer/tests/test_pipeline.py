import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from reasoning.contracts import ContractError
from reasoning.orchestration import MissionOrchestrator, render_instruction
from reasoning.pipeline import MissionPipeline
from reasoning.planner import GenerationError, MissionPlanner
from tests.test_planner import FakeProvider, ROOT, fixtures


def dispatch_fixture():
    """Adapt the existing example to the runtime's conservative task barrier."""
    original = json.loads((ROOT / "mission-orchestrator/examples/dispatch_plan.json").read_text())
    original["steps"][7]["depends_on"] = ["S7"]
    return {
        "allocations": original["allocations"],
        "steps": [{key: value for key, value in step.items() if key != "instruction"}
                  for step in original["steps"]],
        "unassigned": original["unassigned"],
    }


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.request, self.fleet, self.handoff, self.decision = fixtures()
        self.draft = dispatch_fixture()

    def pipeline(self, provider):
        return MissionPipeline(MissionPlanner(provider), MissionOrchestrator(provider))

    def test_connected_pipeline_invokes_both_agents_and_real_validator(self):
        provider = FakeProvider(self.decision, self.draft)
        accepted = []
        result = self.pipeline(provider).run(self.request, self.fleet, on_handoff=accepted.append)
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("You are the Mission Orchestrator", provider.calls[1]["system"])
        context = json.loads(provider.calls[1]["prompt"])
        self.assertEqual(context["mission"], self.handoff)
        self.assertEqual(context["fleet"], self.fleet)
        self.assertIn("worked_example", context)
        self.assertNotIn("instruction", context["worked_example"]["candidate"]["steps"][0])
        self.assertEqual(accepted, [self.handoff])
        self.assertEqual(result.dispatch["world"], self.request.world)
        self.assertEqual(result.dispatch["mission_id"], self.request.mission_id)
        self.assertEqual(len(result.dispatch["steps"]), 19)
        self.assertEqual(result.dispatch["steps"][4]["instruction"], "[search] apple in kitchen")

    def test_saved_handoff_uses_one_call(self):
        provider = FakeProvider(self.draft)
        result = self.pipeline(provider).run(self.request, self.fleet, handoff=self.handoff)
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(provider.calls), 1)

    @patch("reasoning.orchestration.load_worked_example", return_value=None)
    def test_example_is_optional_for_changed_deployments(self, example):
        provider = FakeProvider(self.draft)
        result = self.pipeline(provider).run(self.request, self.fleet, handoff=self.handoff)
        self.assertEqual(result.status, "ready")
        self.assertNotIn("worked_example", json.loads(provider.calls[0]["prompt"]))

    def test_handoff_observer_cannot_change_orchestration_state(self):
        provider = FakeProvider(self.draft)

        def observer(handoff):
            handoff["world"]["open"] = ["kitchen_door"]

        result = self.pipeline(provider).run(self.request, self.fleet, handoff=self.handoff,
                                             on_handoff=observer)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.dispatch["world"]["open"], [])

    def test_orchestration_api_failure_has_no_retry(self):
        provider = FakeProvider(self.decision, GenerationError("Quota exhausted"))
        with self.assertRaises(GenerationError):
            self.pipeline(provider).run(self.request, self.fleet)
        self.assertEqual(len(provider.calls), 2)

    def test_clarification_stops_before_orchestration(self):
        provider = FakeProvider({"status": "needs_clarification", "questions": ["Which room?"]})
        result = self.pipeline(provider).run(self.request, self.fleet)
        self.assertEqual(result.status, "needs_clarification")
        self.assertIsNone(result.dispatch)
        self.assertEqual(len(provider.calls), 1)

    def test_interface_rejects_tampered_handoff_before_orchestration(self):
        wrong_id = deepcopy(self.handoff)
        wrong_id["mission_id"] = "different-mission"
        wrong_world = deepcopy(self.handoff)
        wrong_world["world"]["open"] = ["kitchen_door"]
        cycle = deepcopy(self.handoff)
        cycle["tasks"][0]["depends_on"] = ["T2"]
        for handoff in (wrong_id, wrong_world, cycle):
            with self.subTest(handoff=handoff):
                provider = FakeProvider()
                with self.assertRaises(ContractError):
                    self.pipeline(provider).run(self.request, self.fleet, handoff=handoff)
                self.assertEqual(provider.calls, [])

    def test_dropped_or_duplicate_tasks_and_invalid_allocations_are_rejected(self):
        dropped = deepcopy(self.draft)
        dropped["allocations"].pop()
        duplicate = deepcopy(self.draft)
        duplicate["allocations"].append(duplicate["allocations"][0])
        wrong_robot = deepcopy(self.draft)
        wrong_robot["steps"][0]["robot"] = "turtlebot_1"
        ghost_robot = deepcopy(self.draft)
        ghost_robot["allocations"][0]["robots"].append("ghost")
        duplicate_step = deepcopy(self.draft)
        duplicate_step["steps"][1]["id"] = "S1"
        ghost_entity = deepcopy(self.draft)
        ghost_entity["steps"][4]["args"]["object"] = "invented_apple"
        model_instruction = deepcopy(self.draft)
        model_instruction["steps"][0]["instruction"] = "[grab] juice_bottle"
        wrong_world = {**self.draft, "world": {}}
        for draft in (dropped, duplicate, wrong_robot, ghost_robot, duplicate_step,
                      ghost_entity, model_instruction, wrong_world):
            with self.subTest(draft=draft):
                result = MissionOrchestrator(FakeProvider(draft)).orchestrate(self.handoff, self.fleet)
                self.assertEqual(result.status, "invalid_dispatch")
                self.assertIsNone(result.plan)
                self.assertTrue(result.errors)

    def test_host_inserts_missing_task_completion_barrier(self):
        self.draft["steps"][7]["depends_on"] = ["S5"]
        result = MissionOrchestrator(FakeProvider(self.draft)).orchestrate(self.handoff, self.fleet)
        self.assertEqual(result.status, "ready")
        self.assertIn("S7", result.plan["steps"][7]["depends_on"])
        self.assertIn("S2", result.plan["steps"][7]["depends_on"])

    def test_robot_order_conflicting_with_task_barriers_is_rejected(self):
        steps = self.draft["steps"]
        self.draft["steps"] = steps[7:11] + steps[:7] + steps[11:]
        result = MissionOrchestrator(FakeProvider(self.draft)).orchestrate(self.handoff, self.fleet)
        self.assertEqual(result.status, "invalid_dispatch")
        self.assertIn("cycle", result.errors[0])

    def test_step_cycles_and_missing_dependencies_are_rejected(self):
        for dependencies in (["S19"], ["missing"]):
            with self.subTest(dependencies=dependencies):
                draft = deepcopy(self.draft)
                draft["steps"][0]["depends_on"] = dependencies
                result = MissionOrchestrator(FakeProvider(draft)).orchestrate(self.handoff, self.fleet)
                self.assertEqual(result.status, "invalid_dispatch")

    def test_existing_validator_rejects_payload_violation(self):
        self.handoff["world"]["entities"]["juice_bottle"]["weight_kg"] = 1.0
        result = MissionOrchestrator(FakeProvider(self.draft)).orchestrate(self.handoff, self.fleet)
        self.assertEqual(result.status, "invalid_dispatch")
        self.assertTrue(any("weighs" in error for error in result.errors))

    def test_warnings_are_held_for_review_instead_of_published(self):
        self.draft["steps"][11]["args"]["target"] = "counter"
        result = MissionOrchestrator(FakeProvider(self.draft)).orchestrate(self.handoff, self.fleet)
        self.assertEqual(result.status, "invalid_dispatch")
        self.assertTrue(result.warnings)
        self.assertIsNone(result.plan)

    def test_unknown_initial_room_is_rejected(self):
        self.handoff["world"]["robots"]["drone_1"]["at"] = None
        result = MissionOrchestrator(FakeProvider(self.draft)).orchestrate(self.handoff, self.fleet)
        self.assertEqual(result.status, "invalid_dispatch")
        self.assertIn("initial room", result.errors[0])

    def test_unassigned_tasks_are_reported_without_dispatch(self):
        draft = {"allocations": [], "steps": [], "unassigned": [
            {"task_id": task["task_id"], "reason": "Required capability unavailable"}
            for task in self.handoff["tasks"]
        ]}
        result = self.pipeline(FakeProvider(draft)).run(self.request, self.fleet, handoff=self.handoff)
        self.assertEqual(result.status, "unassigned_tasks")
        self.assertIsNone(result.dispatch)
        self.assertEqual(len(result.report["unassigned"]), 3)

    def test_malformed_json_has_no_dispatch(self):
        result = MissionOrchestrator(FakeProvider("not JSON")).orchestrate(self.handoff, self.fleet)
        self.assertEqual(result.status, "invalid_dispatch")
        self.assertIsNone(result.plan)

    def test_instruction_renderer_uses_fleet_syntax(self):
        robot = self.fleet["robots"][0]
        self.assertEqual(render_instruction(robot["actions"]["search"],
                                            {"object": "apple", "area": "kitchen"}),
                         "[search] apple in kitchen")
