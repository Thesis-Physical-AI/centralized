"""Exercise cross-robot ordering with the actual dispatch simulator."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from structured_mission_interface.interface import load_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dispatch_validator", ROOT / "mission-orchestrator/validate_plan.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class DispatchValidationTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_json(ROOT / "mission-orchestrator/examples/dispatch_plan.json")
        self.fleet = load_json(ROOT / "mission-orchestrator/robots.json")

    def test_independent_tasks_still_validate_without_serializing_all_robots(self):
        errors, warnings = validator.validate(self.plan, self.fleet)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_opening_door_and_crossing_require_an_explicit_order(self):
        self.plan["steps"][3]["depends_on"] = []
        errors, _ = validator.validate(self.plan, self.fleet)
        self.assertTrue(any("unordered shared-state" in error and "open:kitchen_door" in error
                            for error in errors), errors)

    def test_object_handoff_requires_order_even_when_simulation_gets_lucky(self):
        world = self.plan["world"]
        for rid in ("puppypi_1", "turtlebot_1"):
            world["robots"][rid] = {"at": "counter"}
        self.plan["allocations"] = [{"task_id": "T1", "robots": ["puppypi_1", "turtlebot_1"]}]

        def step(sid, robot, action, args, depends_on=()):
            return {"id": sid, "task_id": "T1", "robot": robot, "action": action,
                    "args": args, "depends_on": list(depends_on)}

        self.plan["steps"] = [
            step("A1", "puppypi_1", "grab", {"object": "apple"}),
            step("A2", "puppypi_1", "puton", {"object": "apple", "surface": "counter"}),
            step("B1", "turtlebot_1", "grab", {"object": "apple"}, ["A2"]),
            step("B2", "turtlebot_1", "puton", {"object": "apple", "surface": "counter"}),
        ]
        errors, warnings = validator.validate(deepcopy(self.plan), self.fleet)
        self.assertEqual((errors, warnings), ([], []))
        self.plan["steps"][2]["depends_on"] = []
        errors, _ = validator.validate(deepcopy(self.plan), self.fleet)
        self.assertTrue(any("unordered shared-state" in error and "location:apple" in error
                            for error in errors), errors)

    def test_carrier_location_resolution_does_not_depend_on_registry_order(self):
        self.plan["world"]["robots"]["drone_1"]["at"] = "turtlebot_1"
        robots = {robot["id"]: robot for robot in self.fleet["robots"]}
        world = validator.World(self.plan["world"], robots)
        self.assertEqual(world.state["drone_1"]["room"], "living_room")

    def test_closing_door_must_wait_for_another_robots_crossing(self):
        self.plan["world"]["open"] = ["kitchen_door"]
        self.plan["world"]["robots"]["drone_1"] = {"at": "counter", "airborne": True}
        self.plan["world"]["robots"]["puppypi_1"] = {"at": "kitchen_door", "room": "living_room"}
        self.plan["allocations"] = [{"task_id": "T1", "robots": ["drone_1", "puppypi_1"]}]
        self.plan["steps"] = [
            {"id": "D1", "task_id": "T1", "robot": "drone_1", "action": "movetowards",
             "args": {"target": "landing_pad"}},
            {"id": "P1", "task_id": "T1", "robot": "puppypi_1", "action": "close",
             "args": {"target": "kitchen_door"}},
            {"id": "D2", "task_id": "T1", "robot": "drone_1", "action": "land_on",
             "args": {"surface": "landing_pad"}},
        ]
        errors, _ = validator.validate(deepcopy(self.plan), self.fleet)
        self.assertTrue(any("unordered shared-state" in e and "open:kitchen_door" in e for e in errors))
        self.plan["steps"][1]["depends_on"] = ["D1"]
        self.assertEqual(validator.validate(deepcopy(self.plan), self.fleet), ([], []))

    def test_example_reaches_expected_symbolic_goal_state(self):
        observed = []
        original_world = validator.World

        def capture(*args, **kwargs):
            world = original_world(*args, **kwargs)
            observed.append(world)
            return world

        with patch.object(validator, "World", side_effect=capture):
            self.assertEqual(validator.validate(deepcopy(self.plan), self.fleet), ([], []))
        world = observed[0]
        self.assertEqual(world.parent("apple"), "coffee_table")
        self.assertEqual(world.parent("juice_bottle"), "coffee_table")
        self.assertNotIn("fridge", world.open)
        self.assertFalse(any(state["airborne"] or state["holding"] for state in world.state.values()))
