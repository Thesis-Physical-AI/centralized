from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest


from structured_mission_interface import MissionValidationError, prepare_mission, validate_mission
from structured_mission_interface.__main__ import main
from structured_mission_interface.interface import load_json
from structured_mission_interface.schema import FLEET_SCHEMA, MISSION_SCHEMA

ROOT = Path(__file__).resolve().parents[1]


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.mission = load_json(ROOT / "mission-orchestrator/examples/input.json")
        self.fleet = load_json(ROOT / "mission-orchestrator/robots.json")

    def validate(self):
        return validate_mission(self.mission, self.fleet)

    def assert_code(self, code):
        result = self.validate()
        self.assertFalse(result.valid)
        self.assertIsNone(result.mission)
        self.assertIn(code, {issue.code for issue in result.errors}, result.errors)

    def test_schemas_are_valid(self):
        self.assertEqual(MISSION_SCHEMA["type"], "object")
        self.assertEqual(FLEET_SCHEMA["type"], "object")
        self.assertIn("tasks", MISSION_SCHEMA["required"])
        self.assertIn("robots", FLEET_SCHEMA["required"])
        json.dumps({"mission": MISSION_SCHEMA, "fleet": FLEET_SCHEMA})

    def test_existing_example_and_normalization(self):
        original = deepcopy(self.mission)
        result = self.validate()
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(self.mission, original)
        self.assertEqual(result.mission["tasks"][1]["depends_on"], ["T1"])
        self.assertEqual(result.mission["tasks"][0]["constraints"], {})
        self.assertEqual(result.mission["world"]["passages"], [])
        result.mission["world"]["entities"].clear()
        self.assertTrue(self.mission["world"]["entities"])

    def test_prepare_raises_on_invalid(self):
        self.mission.pop("mission_id")
        with self.assertRaises(MissionValidationError) as caught:
            prepare_mission(self.mission, self.fleet)
        self.assertFalse(caught.exception.result.valid)

    def test_shape_errors_do_not_crash(self):
        for bad in (None, [], "mission", {}, {"tasks": [1]}, {**self.mission, "world": []}):
            with self.subTest(value=bad):
                self.assertFalse(validate_mission(bad, self.fleet).valid)

    def test_duplicate_task(self):
        self.mission["tasks"].append(deepcopy(self.mission["tasks"][0]))
        self.assert_code("duplicate_id")

    def test_unknown_dependency(self):
        self.mission["tasks"][0]["depends_on"] = ["missing"]
        self.assert_code("unknown_dependency")

    def test_task_cycle(self):
        self.mission["tasks"][0]["depends_on"] = ["T2"]
        self.assert_code("dependency_cycle")

    def test_self_dependency(self):
        self.mission["tasks"][0]["depends_on"] = ["T1"]
        self.assert_code("dependency_cycle")

    def test_unknown_constraint_rejected(self):
        self.mission["tasks"][0]["constraints"] = {"unimplemented_safety_rule": True}
        self.assert_code("schema")

    def test_capability_and_payload_constraints(self):
        self.mission["tasks"][0]["constraints"] = {
            "allowed_robots": ["turtlebot_1"], "required_actions": ["grab"], "payload_kg": 0.45}
        self.assertTrue(self.validate().valid)
        self.mission["tasks"][0]["constraints"]["allowed_robots"] = ["drone_1"]
        self.assert_code("unsupported_action")
        self.assert_code("payload_exceeded")

    def test_payload_boundary(self):
        self.mission["tasks"][0]["constraints"] = {"allowed_robots": ["puppypi_1"], "payload_kg": 0.2}
        self.assertTrue(self.validate().valid)
        self.mission["tasks"][0]["constraints"]["payload_kg"] = 0.20001
        self.assert_code("payload_exceeded")

    def test_unknown_and_inactive_allowed_robots(self):
        self.mission["tasks"][0]["constraints"] = {"allowed_robots": ["puppypi_1"]}
        del self.mission["world"]["robots"]["puppypi_1"]
        self.assert_code("unavailable_robot")

    def test_bad_world_references(self):
        self.mission["world"]["entities"]["apple"]["in"] = "missing"
        self.assert_code("unknown_reference")

    def test_location_cycle(self):
        self.mission["world"]["entities"]["counter"]["in"] = "counter"
        self.assert_code("location_cycle")

    def test_robot_location_cycle(self):
        self.mission["world"]["robots"]["drone_1"]["at"] = "drone_1"
        self.assert_code("location_cycle")

    def test_invalid_door_and_passage(self):
        self.mission["world"]["entities"]["kitchen_door"]["connects"] = ["living_room", "apple"]
        self.mission["world"]["passages"] = [["kitchen", "fridge"]]
        self.assert_code("invalid_room")
        self.assert_code("invalid_passage")

    def test_open_must_be_openable(self):
        self.mission["world"]["open"] = ["apple"]
        self.assert_code("invalid_open")

    def test_inconsistent_room(self):
        self.mission["world"]["robots"]["drone_1"]["room"] = "kitchen"
        self.assert_code("inconsistent_location")

    def test_door_side_state(self):
        self.mission["world"]["robots"]["puppypi_1"].update(at="kitchen_door", room="living_room")
        self.assertTrue(self.validate().valid)
        self.mission["world"]["robots"]["puppypi_1"].pop("room")
        self.assert_code("invalid_door_side")

    def test_invalid_holding_and_payload(self):
        world = self.mission["world"]
        world["robots"]["puppypi_1"]["holding"] = "juice_bottle"
        world["entities"]["juice_bottle"]["in"] = "puppypi_1"
        self.assert_code("payload_exceeded")

    def test_cargo_and_holding(self):
        self.mission["world"]["entities"]["apple"]["in"] = "turtlebot_1"
        self.assertTrue(self.validate().valid)
        self.mission["world"]["entities"]["apple"]["in"] = "drone_1"
        self.assert_code("unsupported_cargo")

    def test_robot_without_flight_cannot_be_airborne(self):
        self.mission["world"]["robots"]["puppypi_1"]["airborne"] = True
        self.assert_code("unsupported_flight")

    def test_fleet_duplicate_and_bad_shape(self):
        self.fleet["robots"].append(deepcopy(self.fleet["robots"][0]))
        self.assert_code("duplicate_id")
        self.assertFalse(validate_mission(self.mission, {"robots": None}).valid)

    def test_non_finite_and_boolean_payload_rejected(self):
        for value in (float("nan"), float("inf"), True, -1):
            self.mission["tasks"][0]["constraints"] = {"payload_kg": value}
            self.assertFalse(self.validate().valid)

    def test_cli_writes_only_accepted_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.json", Path(directory) / "validated.json"
            source.write_text(json.dumps(self.mission), encoding="utf-8")
            with redirect_stdout(io.StringIO()) as printed:
                code = main([str(source), "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(printed.getvalue())["valid"])
            self.assertEqual(load_json(output), prepare_mission(self.mission, self.fleet))
            previous = output.read_bytes()
            source.write_text('{"mission_id": "broken"}', encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(source), "--output", str(output)]), 1)
            self.assertEqual(output.read_bytes(), previous)

    def test_cli_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            source.write_text('{"mission_id":"A", "mission_id":"B"}', encoding="utf-8")
            with redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(main([str(source)]), 1)
            self.assertIn("duplicate JSON key", printed.getvalue())

    def test_cli_missing_file(self):
        with redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(main(["missing-input-file.json"]), 1)
        self.assertFalse(json.loads(printed.getvalue())["valid"])

    def test_text_requirements_preserved_with_review_warning(self):
        self.mission["tasks"][0]["constraints"] = {"requirements": ["Keep the fridge closed."]}
        result = self.validate()
        self.assertTrue(result.valid)
        self.assertEqual(result.mission["tasks"][0]["constraints"]["requirements"], ["Keep the fridge closed."])
        self.assertTrue(any("meaning is not machine-verified" in w.message for w in result.warnings))

    def test_snapshot_extensions_and_shorthand_entities(self):
        self.mission["world"]["snapshot_id"] = "snapshot-42"
        self.mission["world"]["robots"]["drone_1"]["battery_percent"] = 80
        self.mission["world"]["entities"]["living_room"] = "room"
        result = self.validate()
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(result.mission["world"]["snapshot_id"], "snapshot-42")
        self.assertEqual(result.mission["world"]["entities"]["living_room"], {"kind": "room"})


if __name__ == "__main__":
    unittest.main()
