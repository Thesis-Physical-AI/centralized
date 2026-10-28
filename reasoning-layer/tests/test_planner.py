import importlib.util
import json
import unittest
from copy import deepcopy
from pathlib import Path

from reasoning.contracts import ContractError, MissionRequest, parse_json, validate_context
from reasoning.planner import GenerationError, MissionPlanner

ROOT = Path(__file__).resolve().parents[2]


def fixtures():
    request = MissionRequest.model_validate_json(
        (ROOT / "reasoning-layer/examples/request.json").read_text()
    )
    fleet = parse_json((ROOT / "mission-orchestrator/robots.json").read_text())
    handoff = parse_json((ROOT / "reasoning-layer/examples/expected_handoff.json").read_text())
    decision = {"status": "ready", "tasks": handoff["tasks"], "questions": [], "reasons": []}
    return request, fleet, handoff, decision


class FakeProvider:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response) if isinstance(response, dict) else response


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.request, self.fleet, self.handoff, self.decision = fixtures()

    def test_matches_existing_orchestrator_input_contract(self):
        provider = FakeProvider(self.decision)
        outcome = MissionPlanner(provider).plan(self.request, self.fleet)
        self.assertEqual(outcome.handoff, self.handoff)
        self.assertEqual(set(outcome.handoff), {"mission_id", "tasks", "world"})
        self.assertEqual(outcome.attempts, 1)
        context = json.loads(provider.calls[0]["prompt"])
        self.assertEqual(context["fleet"], self.fleet)
        self.assertEqual(context["world"], self.request.world)

    def test_world_extensions_are_preserved_and_handoff_is_independent(self):
        self.request.world["snapshot_id"] = "state-42"
        self.request.world["robots"]["drone_1"]["battery_percent"] = 80
        result = MissionPlanner(FakeProvider(self.decision)).plan(self.request, self.fleet)
        self.assertEqual(result.handoff["world"], self.request.world)
        result.handoff["world"]["robots"]["drone_1"]["at"] = "kitchen"
        self.assertEqual(self.request.world["robots"]["drone_1"]["at"], "landing_pad")

    def test_rejects_model_attempt_to_replace_world_or_identity(self):
        for key, value in (("world", {}), ("mission_id", "injected")):
            with self.subTest(key=key):
                bad = {**self.decision, key: value}
                with self.assertRaises(GenerationError):
                    MissionPlanner(FakeProvider(bad), max_attempts=1).plan(self.request, self.fleet)

    def test_rejects_task_level_robot_assignments(self):
        self.decision["tasks"][0]["robot"] = "drone_1"
        with self.assertRaises(GenerationError):
            MissionPlanner(FakeProvider(self.decision), max_attempts=1).plan(self.request, self.fleet)

    def test_invalid_graphs_are_not_handed_off(self):
        duplicate = deepcopy(self.decision)
        duplicate["tasks"][1]["task_id"] = "T1"
        unknown = deepcopy(self.decision)
        unknown["tasks"][1]["depends_on"] = ["missing"]
        self_dep = deepcopy(self.decision)
        self_dep["tasks"][0]["depends_on"] = ["T1"]
        cycle = deepcopy(self.decision)
        cycle["tasks"][0]["depends_on"] = ["T2"]
        repeated = deepcopy(self.decision)
        repeated["tasks"][1]["depends_on"] = ["T1", "T1"]
        for bad in (duplicate, unknown, self_dep, cycle, repeated):
            with self.subTest(bad=bad):
                with self.assertRaises(GenerationError):
                    MissionPlanner(FakeProvider(bad), max_attempts=1).plan(self.request, self.fleet)

    def test_one_repair_with_validation_feedback(self):
        bad = deepcopy(self.decision)
        bad["tasks"][1]["depends_on"] = ["missing"]
        provider = FakeProvider(bad, self.decision)
        outcome = MissionPlanner(provider, max_attempts=2).plan(self.request, self.fleet)
        self.assertEqual(outcome.handoff, self.handoff)
        self.assertEqual(outcome.attempts, 2)
        repaired = json.loads(provider.calls[1]["prompt"])
        self.assertTrue(repaired["repair"]["errors"])
        self.assertEqual(repaired["original_request"]["world"], self.request.world)

    def test_stops_after_two_invalid_responses(self):
        provider = FakeProvider("not JSON", "not JSON", self.decision)
        with self.assertRaises(GenerationError):
            MissionPlanner(provider, max_attempts=2).plan(self.request, self.fleet)
        self.assertEqual(len(provider.calls), 2)

    def test_no_format_retry_for_provider_failure(self):
        provider = FakeProvider(GenerationError("request failed"), self.decision)
        with self.assertRaises(GenerationError):
            MissionPlanner(provider).plan(self.request, self.fleet)
        self.assertEqual(len(provider.calls), 1)

    def test_non_ready_decisions_have_no_handoff(self):
        for decision in (
            {"status": "needs_clarification", "tasks": [], "questions": ["Which room?"], "reasons": []},
            {"status": "infeasible", "tasks": [], "questions": [], "reasons": ["No lifting capability."]},
        ):
            with self.subTest(status=decision["status"]):
                result = MissionPlanner(FakeProvider(decision)).plan(self.request, self.fleet)
                self.assertIsNone(result.handoff)

    def test_inconsistent_decisions_are_rejected(self):
        for decision in (
            {"status": "ready", "tasks": []},
            {"status": "needs_clarification", "tasks": self.decision["tasks"], "questions": ["Where?"]},
            {"status": "infeasible", "reasons": []},
            {"status": "needs_clarification", "questions": ["  "]},
        ):
            with self.subTest(decision=decision):
                with self.assertRaises(GenerationError):
                    MissionPlanner(FakeProvider(decision), max_attempts=1).plan(self.request, self.fleet)

    def test_bad_context_is_rejected_before_model_call(self):
        worlds = []
        missing = deepcopy(self.request.world)
        del missing["entities"]
        worlds.append(missing)
        cyclic = deepcopy(self.request.world)
        cyclic["entities"]["counter"]["in"] = "apple"
        worlds.append(cyclic)
        unknown = deepcopy(self.request.world)
        unknown["robots"]["invented_robot"] = {"at": "kitchen"}
        worlds.append(unknown)
        bad_parent = deepcopy(self.request.world)
        bad_parent["entities"]["counter"]["in"] = []
        worlds.append(bad_parent)
        for world in worlds:
            with self.subTest(world=world):
                provider = FakeProvider(self.decision)
                request = self.request.model_copy(update={"world": world})
                with self.assertRaises(ContractError):
                    MissionPlanner(provider).plan(request, self.fleet)
                self.assertEqual(provider.calls, [])

    def test_duplicate_keys_and_non_finite_numbers_are_rejected(self):
        for text in ('{"id": 1, "id": 2}', '{"value": NaN}', '{"value": Infinity}'):
            with self.subTest(text=text), self.assertRaises(ContractError):
                parse_json(text)

    def test_existing_dispatch_validator_accepts_unchanged_example(self):
        path = ROOT / "mission-orchestrator/validate_plan.py"
        spec = importlib.util.spec_from_file_location("dispatch_validator", path)
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        dispatch = parse_json((ROOT / "mission-orchestrator/examples/dispatch_plan.json").read_text())
        # Consume the candidate's exact world, as the orchestrator's skill specifies.
        dispatch["world"] = self.handoff["world"]
        self.assertEqual({task["task_id"] for task in self.handoff["tasks"]},
                         {allocation["task_id"] for allocation in dispatch["allocations"]})
        errors, warnings = validator.validate(dispatch, self.fleet)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
