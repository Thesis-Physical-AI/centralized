"""Generate the Isaac Sim scene file (decentralised/sim/worlds format) from a dashboard world.

The planner's world is symbolic (entities, kinds, containment, robots). The simulator also needs a
layout: wall size, positions, approach poses, primitive shapes and the phrase the onboard VLM is asked
about ("a red ball"). This module derives that layout automatically:

- Rooms are 4 x 4 m zones placed side by side inside one walled arena (no interior walls: the
  prototype navigator drives in straight lines).
- Each room's approach pose is its centre, so "search <room>" drives there first.
- Things directly in a room sit on a ring around the room centre, each with an approach pose
  0.65 m in front of it, facing it. Things on a surface/container sit on top of it.
- Every entity gets a distinct colour/shape/size, so its VLM phrase is unique in the scene.

Per-world `sim` overrides (kept outside the planner's world so Gemini never sees them) replace
any generated value: {"room": {...}, "robot_start": [x, y, yaw_deg],
"entities": {name: {"position", "approach", "shape", "description", "present_in_sim"}}}.
An override value of null removes the generated value (e.g. no approach pose).
"""
from copy import deepcopy
import math

SIM_ROBOT = "puppypi_1"  # the only robot model the Isaac Sim prototype loads
ROOM_SIZE = 4.0
RING_RADIUS = 1.4
APPROACH_GAP = 0.65
WALLS = {"wall_height": 0.25, "wall_color": [0.75, 0.75, 0.72]}
OVERRIDABLE = {"position", "approach", "shape", "description", "present_in_sim"}

# Distinct palettes per shape, so no two entities share a phrase until a palette is exhausted.
BALLS = [("red", [0.9, 0.05, 0.05]), ("yellow", [0.95, 0.85, 0.1]), ("green", [0.1, 0.7, 0.2]),
         ("orange", [1.0, 0.5, 0.0]), ("purple", [0.5, 0.1, 0.7]), ("pink", [1.0, 0.4, 0.7]),
         ("white", [0.95, 0.95, 0.95]), ("black", [0.05, 0.05, 0.05])]
SURFACES = [("brown", [0.55, 0.35, 0.2]), ("grey", [0.5, 0.5, 0.5]), ("beige", [0.85, 0.75, 0.55]),
            ("dark brown", [0.3, 0.18, 0.1])]
CONTAINERS = [("blue", [0.1, 0.2, 0.8]), ("teal", [0.0, 0.5, 0.5]), ("cyan", [0.2, 0.8, 0.9]),
              ("navy", [0.05, 0.08, 0.35])]
SIZES = ["", "large ", "small "]


class SceneError(ValueError):
    pass


class NoSimRobot(SceneError):
    """The world is fine, it just has no robot the simulator can play."""


def _r(value):
    return round(value, 3)


def _top(spec):
    """Height of the top of a placed entity."""
    shape, z = spec["shape"], spec["position"][2]
    return z + (shape["size"][2] / 2 if shape["type"] == "box" else shape["radius"])


def generate_scene(world, overrides=None):
    if overrides is not None and not isinstance(overrides, dict):
        raise SceneError("sim overrides must be an object")
    overrides = {k: v for k, v in (overrides or {}).items() if not k.startswith("_")}  # "_comment" etc.
    if set(overrides) - {"room", "robot_start", "entities"}:
        raise SceneError("sim overrides may only contain room, robot_start and entities")
    entity_overrides = overrides.get("entities") or {}
    if not isinstance(entity_overrides, dict):
        raise SceneError("sim entity overrides must be an object")
    for name, extra in entity_overrides.items():
        if not isinstance(extra, dict) or set(extra) - OVERRIDABLE:
            raise SceneError(f"sim override for '{name}' may only set {', '.join(sorted(OVERRIDABLE))}")
    if SIM_ROBOT not in world.get("robots", {}):
        raise NoSimRobot(f"Isaac Sim needs robot '{SIM_ROBOT}' in the world")

    entities = {name: ({"kind": spec} if isinstance(spec, str) else deepcopy(spec))
                for name, spec in world["entities"].items()}
    robots = world["robots"]
    rooms = [name for name, spec in entities.items() if spec["kind"] == "room"] or ["_arena"]
    centres = {room: ((i - (len(rooms) - 1) / 2) * ROOM_SIZE, 0.0) for i, room in enumerate(rooms)}

    def room_of(name, seen=()):
        spec = entities.get(name)
        if spec is None or name in seen:
            return rooms[0]
        if spec["kind"] == "room":
            return name
        parent = spec.get("in")
        return rooms[0] if parent is None or parent in robots else room_of(parent, (*seen, name))

    palettes = {"ball": iter([(f"{size}{word}", rgb, i) for i, size in enumerate(SIZES) for word, rgb in BALLS]),
                "surface": iter([(f"{size}{word}", rgb, i) for i, size in enumerate(SIZES) for word, rgb in SURFACES]),
                "container": iter([(f"{size}{word}", rgb, i) for i, size in enumerate(SIZES) for word, rgb in CONTAINERS])}

    def look(kind):
        family = {"surface": "surface", "container": "container"}.get(kind, "ball")
        word, rgb, size = next(palettes[family], ("grey", [0.5, 0.5, 0.5], 0))
        scale = [1.0, 1.4, 0.7][size]
        if family == "surface":
            shape = {"type": "box", "size": [_r(0.4 * scale), _r(0.6 * scale), 0.08], "color": rgb}
        elif family == "container":
            shape = {"type": "box", "size": [_r(0.3 * scale)] * 2 + [_r(0.25 * scale)], "color": rgb}
        else:
            shape = {"type": "sphere", "radius": _r(0.05 * scale), "color": rgb}
        return shape, f"a {word} {'ball' if shape['type'] == 'sphere' else 'box'}"

    scene = {}
    for name, spec in entities.items():
        out = {key: value for key, value in spec.items()}
        if spec["kind"] == "room":
            out["description"] = "a room"
            out["approach"] = [_r(centres[name][0]), _r(centres[name][1]), 0.0]  # drive to its centre to search it
        elif spec["kind"] == "door" or spec.get("in") in robots:
            out["description"] = name.replace("_", " ")  # doors and carried items are not modelled
            out["present_in_sim"] = False
        else:
            out["shape"], out["description"] = look(spec["kind"])
        scene[name] = out

    # Ring placement for things directly in a room (or with no parent); stacking for the rest.
    on_ring = {room: [] for room in rooms}
    stacked = {}
    for name, spec in entities.items():
        if "shape" not in scene[name]:
            continue
        parent = spec.get("in")
        if parent in entities and entities[parent]["kind"] != "room":
            stacked.setdefault(parent, []).append(name)
        else:
            on_ring[room_of(name)].append(name)
    for room, names in on_ring.items():
        cx, cy = centres[room]
        for i, name in enumerate(names):
            angle = 2 * math.pi * i / len(names) + math.pi / 4
            ux, uy = math.cos(angle), math.sin(angle)
            spec = scene[name]
            half = spec["shape"]["size"][2] / 2 if spec["shape"]["type"] == "box" else spec["shape"]["radius"]
            spec["position"] = [_r(cx + RING_RADIUS * ux), _r(cy + RING_RADIUS * uy), _r(half)]
            gap = RING_RADIUS - APPROACH_GAP
            spec["approach"] = [_r(cx + gap * ux), _r(cy + gap * uy), _r(math.degrees(angle))]

    def place_on(parent, seen=()):
        if parent in seen:
            return
        base = scene[parent]
        if "position" not in base:  # parent is itself stacked: place it first
            grand = entities[parent].get("in")
            if grand in stacked and parent in stacked[grand]:
                place_on(grand, (*seen, parent))
        if "position" not in base:
            return
        children = stacked.get(parent, [])
        for j, name in enumerate(children):
            spec = scene[name]
            if "position" in spec:
                continue
            half = spec["shape"]["size"][2] / 2 if spec["shape"]["type"] == "box" else spec["shape"]["radius"]
            offset = (j - (len(children) - 1) / 2) * 0.12
            spec["position"] = [_r(base["position"][0] + offset), base["position"][1], _r(_top(base) + half)]
            if "approach" in base:
                spec["approach"] = list(base["approach"])

    for parent in stacked:
        place_on(parent)

    for name, extra in entity_overrides.items():
        if name in scene:
            for key, value in extra.items():  # null removes a generated value
                if value is None:
                    scene[name].pop(key, None)
                else:
                    scene[name][key] = deepcopy(value)
    for name, spec in scene.items():
        if spec.get("present_in_sim", True) and "shape" in spec and "position" not in spec:
            spec["present_in_sim"] = False  # unreachable stacking (e.g. a cycle): leave it out of the scene

    start = overrides.get("robot_start")
    if start is None:
        at = robots[SIM_ROBOT].get("at")
        target = scene.get(at, {})
        if at in centres:
            start = [*centres[at], 0.0]
        elif "approach" in target:
            start = list(target["approach"])
        else:
            start = [*centres[room_of(at)], 0.0]
    room = {"size": [ROOM_SIZE * len(rooms), ROOM_SIZE], **WALLS, **(overrides.get("room") or {})}
    return {"robot": {"id": SIM_ROBOT, "start": start}, "room": room, "entities": scene}
