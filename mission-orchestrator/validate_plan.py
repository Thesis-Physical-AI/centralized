#!/usr/bin/env python3
"""Validate a mission orchestrator dispatch plan.

Static checks: every step uses a robot from the fleet, an action that robot
supports, and arguments of the right kind; dependencies form a DAG.
Dynamic checks: the world is simulated step by step (rooms, doors, object
locations, grippers, payload, cargo trays, drone flight state) to catch
pre-condition violations.

Usage: python3 validate_plan.py <plan.json>
Exit code 0 if the plan is valid (warnings allowed), 1 otherwise.
"""
import json
import sys
from collections import deque
from pathlib import Path

ROBOTS = Path(__file__).resolve().parent / "robots.json"
OBJECT_KINDS = {"object", "container", "door"}
MANIPULATION = {"open", "close", "grab", "putinto", "puton", "push"}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def kind_matches(kind, allowed):
    return ("any" in allowed or kind in allowed
            or ("object" in allowed and kind in OBJECT_KINDS))


def topo_order(steps, errors):
    """Order steps by depends_on plus implicit per-robot ordering (list order)."""
    ids = [s["id"] for s in steps]
    deps = {s["id"]: set(s.get("depends_on", [])) for s in steps}
    last_by_robot = {}
    for s in steps:
        prev = last_by_robot.get(s["robot"])
        if prev:
            deps[s["id"]].add(prev)
        last_by_robot[s["robot"]] = s["id"]
    for sid, ds in deps.items():
        for d in ds - set(ids):
            errors.append(f"{sid}: depends on unknown step '{d}'")
        ds &= set(ids)

    order, done = [], set()
    while len(order) < len(ids):
        ready = [i for i in ids if i not in done and deps[i] <= done]
        if not ready:
            errors.append("dependency cycle among steps: "
                          + ", ".join(i for i in ids if i not in done))
            return order
        for i in ready:
            order.append(i)
            done.add(i)
    return order


class World:
    """Simulated world state: entity locations, open doors/containers, robots."""

    def __init__(self, world, robots):
        self.robots = robots
        self.entities = {}
        for name, e in world.get("entities", {}).items():
            self.entities[name] = {"kind": e} if isinstance(e, str) else dict(e)
        for rid in robots:
            self.entities.setdefault(rid, {"kind": "robot"})
        self.open = set(world.get("open", []))
        self.passages = [tuple(p) for p in world.get("passages", [])]
        self.state = {}
        for rid, r in robots.items():
            init = world.get("robots", {}).get(rid, {})
            self.state[rid] = {
                "at": init.get("at"),
                "room": init.get("room"),
                "holding": init.get("holding"),
                "airborne": init.get("airborne", False),
            }
            if self.state[rid]["room"] is None and init.get("at"):
                self.state[rid]["room"] = self.room_of(init["at"])

    def kind(self, name):
        if name in self.robots:
            return "carrier" if self.robots[name].get("cargo") else "robot"
        return self.entities.get(name, {}).get("kind")

    def parent(self, name):
        return self.entities.get(name, {}).get("in")

    def room_of(self, name, _depth=0):
        if name is None or _depth > 20:
            return None
        if name in self.robots:
            return self.state[name]["room"]
        if self.kind(name) == "room":
            return name
        return self.room_of(self.parent(name), _depth + 1)

    def ancestors(self, name):
        chain, seen = [], set()
        while name and name not in seen:
            seen.add(name)
            chain.append(name)
            name = self.parent(name)
        return chain

    def path_exists(self, start, goal):
        """BFS between rooms through open doors and always-open passages."""
        if start is None or goal is None or start == goal:
            return True
        edges = list(self.passages)
        for name, e in self.entities.items():
            if e.get("kind") == "door" and name in self.open and len(e.get("connects", [])) == 2:
                edges.append(tuple(e["connects"]))
        seen, queue = {start}, deque([start])
        while queue:
            room = queue.popleft()
            for a, b in edges:
                for x, y in ((a, b), (b, a)):
                    if x == room and y not in seen:
                        if y == goal:
                            return True
                        seen.add(y)
                        queue.append(y)
        return False

    def closed_doors_between(self, start, goal):
        return [n for n, e in self.entities.items()
                if e.get("kind") == "door" and n not in self.open
                and set(e.get("connects", [])) & {start, goal}]


def validate(plan, fleet):
    errors, warnings = [], []
    robots = {r["id"]: r for r in fleet["robots"]}
    capabilities = {rid: r["actions"] for rid, r in robots.items()}
    w = World(plan.get("world", {}), robots)
    has_world = bool(plan.get("world", {}).get("entities"))

    steps = plan.get("steps", [])
    seen = set()
    for s in steps:
        if s["id"] in seen:
            errors.append(f"duplicate step id '{s['id']}'")
        seen.add(s["id"])
    by_id = {s["id"]: s for s in steps}
    task_ids = {a["task_id"] for a in plan.get("allocations", [])}

    # Static checks: robot, action, argument names and kinds.
    static_ok = set()
    for s in steps:
        sid, rid, action, args = s["id"], s["robot"], s["action"], s.get("args", {})
        if rid not in robots:
            errors.append(f"{sid}: unknown robot '{rid}'")
            continue
        spec = capabilities[rid].get(action)
        if spec is None:
            errors.append(f"{sid}: {rid} cannot '{action}'; "
                          f"available: {', '.join(sorted(capabilities[rid]))}")
            continue
        if set(spec["args"]) != set(args):
            errors.append(f"{sid}: '{action}' expects args {sorted(spec['args'])}, got {sorted(args)}")
            continue
        kind_ok = True
        for arg, value in args.items():
            kind = w.kind(value)
            if kind is None:
                if has_world:
                    warnings.append(f"{sid}: '{value}' is not a known entity in world.entities")
            elif not kind_matches(kind, spec["args"][arg]):
                errors.append(f"{sid}: {rid} cannot '{action}' a {kind} ('{value}'); "
                              f"{arg} must be one of {spec['args'][arg]}")
                kind_ok = False
        if task_ids and s.get("task_id") not in task_ids:
            warnings.append(f"{sid}: task_id '{s.get('task_id')}' has no allocation entry")
        if kind_ok:
            static_ok.add(sid)

    # Dynamic checks: simulate in dependency order.
    for sid in topo_order(steps, errors):
        if sid not in static_ok:
            continue
        s = by_id[sid]
        rid, action, args = s["robot"], s["action"], s.get("args", {})
        st = w.state[rid]
        robot = robots[rid]
        is_drone = "takeoff_from" in robot["actions"]

        def err(msg):
            errors.append(f"{sid}: {msg}")

        def warn(msg):
            warnings.append(f"{sid}: {msg}")

        def require_near(target):
            if rid in w.ancestors(target):
                return  # on its own cargo tray
            room = w.room_of(target)
            if room and st["room"] and room != st["room"]:
                err(f"{rid} is in '{st['room']}' but '{target}' is in '{room}'")
            elif st["at"] not in w.ancestors(target):
                warn(f"{rid} is at '{st['at']}', not at '{target}' before '{action}' "
                     f"(add a movetowards step?)")

        def require_free_gripper():
            if st["holding"]:
                err(f"{rid} is holding '{st['holding']}' and cannot {action}")

        if is_drone and action != "takeoff_from" and not st["airborne"]:
            err(f"{rid} must take off before '{action}'")

        if action == "takeoff_from":
            if st["airborne"]:
                err(f"{rid} is already airborne")
            elif st["at"] and st["at"] != args["surface"]:
                warn(f"{rid} is on '{st['at']}', not '{args['surface']}'")
            st["airborne"] = True

        elif action == "land_on":
            if st["at"] != args["surface"]:
                warn(f"{rid} should movetowards '{args['surface']}' before landing")
            st["airborne"] = False
            st["at"] = args["surface"]
            st["room"] = w.room_of(args["surface"]) or st["room"]

        elif action in ("movetowards", "dock"):
            target = args.get("target") or args.get("station")
            if target == rid:
                err(f"{rid} cannot move towards itself")
                continue
            if w.kind(target) == "door":
                sides = w.entities[target].get("connects", [])
                goal = st["room"] if st["room"] in sides else (sides[0] if sides else None)
            else:
                goal = w.room_of(target)
            if not w.path_exists(st["room"], goal):
                blocking = w.closed_doors_between(st["room"], goal)
                err(f"{rid} cannot reach '{goal}' from '{st['room']}'; closed door(s): "
                    f"{', '.join(blocking) or 'none known, no connection'}")
            st["at"] = target
            st["room"] = goal or st["room"]
            # Objects on a cargo tray travel with the robot (rooms resolve dynamically).

        elif action in ("search", "inspect"):
            area = args.get("area") or args.get("target")
            room = w.room_of(area)
            if room and st["room"] and room != st["room"]:
                warn(f"{rid} is in '{st['room']}' but '{area}' is in '{room}'")

        elif action in ("open", "close"):
            target = args["target"]
            require_near(target)
            require_free_gripper()
            if action == "open":
                if target in w.open:
                    warn(f"'{target}' is already open")
                w.open.add(target)
            else:
                if target not in w.open:
                    warn(f"'{target}' is already closed")
                w.open.discard(target)

        elif action == "push":
            require_near(args["object"])

        elif action == "grab":
            obj = args["object"]
            require_near(obj)
            require_free_gripper()
            holder = w.parent(obj)
            if holder in w.robots and w.state.get(holder, {}).get("holding") == obj:
                err(f"'{obj}' is held by {holder}")
            for anc in w.ancestors(obj)[1:]:
                if w.kind(anc) == "container" and anc not in w.open:
                    err(f"'{obj}' is inside closed container '{anc}'")
                    break
            weight = w.entities.get(obj, {}).get("weight_kg")
            limit = robot.get("max_payload_kg")
            if weight is not None and limit is not None and weight > limit:
                err(f"'{obj}' weighs {weight} kg; {rid} can lift at most {limit} kg")
            st["holding"] = obj
            w.entities.setdefault(obj, {"kind": "object"})["in"] = rid

        elif action in ("putinto", "puton"):
            obj = args["object"]
            place = args.get("container") or args.get("surface")
            if st["holding"] != obj:
                err(f"{rid} is not holding '{obj}' (holding: {st['holding']})")
            if place == rid:
                if action == "puton" and robot.get("cargo"):
                    pass  # loading its own tray
                else:
                    err(f"{rid} cannot {action} onto itself")
            else:
                require_near(place)
            if action == "putinto" and place not in w.open:
                err(f"container '{place}' is not open")
            st["holding"] = None
            w.entities.setdefault(obj, {"kind": "object"})["in"] = place

    for rid, st in w.state.items():
        if st["airborne"]:
            warnings.append(f"end of plan: {rid} is still airborne (add a land_on step?)")
        if st["holding"]:
            warnings.append(f"end of plan: {rid} is still holding '{st['holding']}'")

    return errors, warnings


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    plan = load_json(sys.argv[1])
    errors, warnings = validate(plan, load_json(ROBOTS))
    for msg in warnings:
        print(f"WARNING: {msg}")
    for msg in errors:
        print(f"ERROR: {msg}")
    print("VALID" if not errors else f"INVALID ({len(errors)} error(s))")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
