import json
from pathlib import Path
import unittest

from sim_scene import NoSimRobot, SceneError, generate_scene

ROOT = Path(__file__).resolve().parents[1]


class SimSceneTests(unittest.TestCase):
    def test_demo_world_regenerates_the_hand_written_one_room_scene(self):
        demo = json.loads((ROOT / "worlds/demo_living_room.json").read_text())
        reference = json.loads((ROOT.parent / "decentralised/sim/worlds/one_room.json").read_text())
        reference.pop("_comment")
        reference["entities"]["toy"].pop("_note")
        self.assertEqual(generate_scene(demo["world"], demo["sim"]), reference)

    def test_generated_layout_is_complete_and_every_phrase_is_unique(self):
        world = {"entities": {"kitchen": {"kind": "room"}, "hall": {"kind": "room"},
                              "counter": {"kind": "surface", "in": "kitchen"},
                              "mug": {"kind": "object", "in": "counter"}, "keys": {"kind": "object", "in": "counter"},
                              "bin": {"kind": "container", "in": "hall"}, "ball": {"kind": "object", "in": "hall"},
                              "shelf": "surface", "door_1": {"kind": "door", "connects": ["kitchen", "hall"]},
                              "badge": {"kind": "object", "in": "puppypi_1"}},
                 "robots": {"puppypi_1": {"at": "hall"}}}
        scene = generate_scene(world)
        self.assertEqual(scene["room"]["size"], [8.0, 4.0])
        visible = {n: e for n, e in scene["entities"].items() if e.get("present_in_sim", True) and "shape" in e}
        self.assertEqual(set(visible), {"counter", "mug", "keys", "bin", "ball", "shelf"})
        self.assertFalse(scene["entities"]["door_1"]["present_in_sim"])
        self.assertFalse(scene["entities"]["badge"]["present_in_sim"])  # carried by the robot
        phrases = [e["description"] for e in visible.values()]
        self.assertEqual(len(phrases), len(set(phrases)))
        # The kitchen is the left zone, the hall the right one; the robot starts in the hall's centre.
        self.assertLess(scene["entities"]["counter"]["position"][0], 0)
        self.assertGreater(scene["entities"]["bin"]["position"][0], 0)
        self.assertEqual(scene["robot"], {"id": "puppypi_1", "start": [2.0, 0.0, 0.0]})
        self.assertEqual(scene["entities"]["kitchen"]["approach"], [-2.0, 0.0, 0.0])
        counter, mug = scene["entities"]["counter"], scene["entities"]["mug"]
        self.assertAlmostEqual(mug["position"][2], 0.08 + 0.05)  # resting on the counter top
        self.assertEqual(mug["approach"], counter["approach"])
        for spec in visible.values():
            self.assertLessEqual(abs(spec["position"][0]), 4.0)
            self.assertLessEqual(abs(spec["position"][1]), 2.0)

    def test_overrides_pin_values_and_null_removes_them(self):
        world = {"entities": {"room": {"kind": "room"}, "ball": {"kind": "object", "in": "room"}},
                 "robots": {"puppypi_1": {"at": "room"}}}
        scene = generate_scene(world, {"_comment": "ignored", "robot_start": [1, 1, 90],
                                       "entities": {"ball": {"description": "a tennis ball", "approach": None,
                                                             "present_in_sim": False}}})
        ball = scene["entities"]["ball"]
        self.assertEqual(ball["description"], "a tennis ball")
        self.assertNotIn("approach", ball)
        self.assertFalse(ball["present_in_sim"])
        self.assertEqual(scene["robot"]["start"], [1, 1, 90])

    def test_rejects_worlds_without_the_sim_robot_and_bad_overrides(self):
        world = {"entities": {"room": {"kind": "room"}}, "robots": {"turtlebot_1": {"at": "room"}}}
        with self.assertRaises(NoSimRobot):
            generate_scene(world)
        world["robots"] = {"puppypi_1": {"at": "room"}}
        for bad in ([], {"walls": 1}, {"entities": {"room": {"colour": "red"}}}):
            with self.subTest(bad=bad), self.assertRaises(SceneError):
                generate_scene(world, bad)


if __name__ == "__main__":
    unittest.main()
