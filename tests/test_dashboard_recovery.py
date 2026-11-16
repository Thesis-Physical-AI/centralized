"""Full revision/clarification coordinator against the real edge mailbox, without GPU/API calls."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
import time
import unittest

from mission_dashboard import Console, write_json
from reasoning.contracts import MissionRequest
from reasoning.orchestration import MissionOrchestrator
from reasoning.planner import MissionPlanner

EDGE = Path(__file__).resolve().parents[2] / "decentralised"
if EDGE.exists():
    sys.path.insert(0, str(EDGE))
    from puppy_agent.session import ExecutionSession, Mailbox


class Provider:
    def __init__(self, response):
        self.response = response

    def generate(self, **kwargs):
        return json.dumps(self.response)


def report(plan, revision, session):
    statuses = ["SUCCEEDED", "BLOCKED", "NOT_DISPATCHED"] if revision == 0 else ["SUCCEEDED"] * len(plan["steps"])
    results = []
    for step, status in zip(plan["steps"], statuses):
        base = {"step_id": step["id"], "robot_id": step["robot"], "status": status}
        if status != "NOT_DISPATCHED":
            base.update(mission_id=plan["mission_id"], plan_revision=revision, attempt=1,
                        reason_code="" if status == "SUCCEEDED" else "OBJECT_NOT_FOUND", observations=[])
        results.append(base)
    return {"mission_id": plan["mission_id"], "execution": {"mission_id": plan["mission_id"],
            "plan_revision": revision, "status": "HALTED" if revision == 0 else "SUCCEEDED", "steps": results},
            "checkpoint": session.state()}


class ActorProcess:
    def __init__(self, directory, plan):
        self.done, self.stop = Event(), Event()
        self.error = None
        self.thread = Thread(target=self.run, args=(directory, plan), daemon=True)
        self.thread.start()

    def run(self, directory, plan):
        try:
            mailbox = Mailbox(directory)
            session = ExecutionSession(plan["mission_id"])
            session.idle(pose=[0, 0, .1], sim_time=15)
            write_json(mailbox.root / "reports/0.json", report(plan, 0, session))
            mailbox.publish(session)
            while not self.stop.is_set() and session.phase != "CLOSED":
                manager = mailbox.next_command(session)
                if manager:
                    # The exact mailbox contract is exercised; robot motion itself is tested separately.
                    replacement = json.loads(next(p for p in (mailbox.root / "commands").glob("*.json")
                                                  if json.loads(p.read_text()).get("action") == "activate").read_text())["dispatch"]
                    session.idle(pose=[0, 0, .1], sim_time=20)
                    write_json(mailbox.root / "reports/1.json", report(replacement, 1, session))
                    mailbox.publish(session)
                time.sleep(.01)
        except Exception as error:
            self.error = error
        finally:
            self.done.set()

    def poll(self):
        return (1 if self.error else 0) if self.done.is_set() else None

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise TimeoutError("test actor did not stop")
        return self.poll()


class RecoveryConsole(Console):
    def __init__(self, output, *, clarification=True, unchanged=False):
        super().__init__(output)
        self.ask = clarification
        self.unchanged = unchanged
        self.generation_calls = 0

    def start_simulator(self, command, folder):
        self.actor = ActorProcess(folder / "session", json.loads((folder / "dispatch.json").read_text()))
        return self.actor

    @staticmethod
    def terminate_process(process):
        process.stop.set()
        process.thread.join(3)

    def command(self, args, cwd, folder, name, timeout):
        if name == "planning":
            request = json.loads((folder / "request.json").read_text())
            world, mid = request["world"], request["mission_id"]
            steps = [{"id": "S1", "task_id": "T1", "robot": "puppypi_1", "action": "search", "args": {"object": "apple", "area": "living_room"}},
                     {"id": "S2", "task_id": "T2", "robot": "puppypi_1", "action": "search", "args": {"object": "toy", "area": "living_room"}},
                     {"id": "S3", "task_id": "T3", "robot": "puppypi_1", "action": "inspect", "args": {"target": "table"}, "depends_on": ["S2"]}]
            write_json(folder / "dispatch.json", {"mission_id": mid, "world": world, "steps": steps, "unassigned": []})
            return 0
        assert name.startswith("replanning-")
        values = [str(a) for a in args]
        def read(flag):
            return json.loads(Path(values[values.index(flag) + 1]).read_text())
        self.generation_calls += 1
        if self.ask and self.generation_calls == 1:
            decision = {"status": "needs_clarification", "tasks": [], "questions": ["Where else should we search?"], "reasons": []}
        else:
            decision = {"status": "ready", "tasks": [
                {"task_id": "R1_T2", "description": "Search the known crate for the toy", "constraints": {}},
                {"task_id": "R1_T3", "description": "Inspect the table", "depends_on": ["R1_T2"], "constraints": {}}],
                "questions": [], "reasons": [], "objective_coverage": {"R1_T2": ["T2"], "R1_T3": ["T3"]}}
        proposal = MissionPlanner(Provider(decision)).replan(MissionRequest.model_validate(read("--input")), self.fleet(),
            dispatch=read("--dispatch"), report=read("--report"), plan_revision=int(values[values.index("--plan-revision") + 1]),
            history=read("--history"), clarification=read("--clarification")["text"])
        if proposal["handoff"]:
            draft = {"allocations": [{"task_id": t, "robots": ["puppypi_1"], "rationale": "Recover remaining goal"}
                                      for t in ("R1_T2", "R1_T3")], "unassigned": [], "steps": [
                {"id": "N1", "task_id": "R1_T2", "robot": "puppypi_1", "action": "search",
                 "args": {"object": "toy", "area": "living_room" if self.unchanged else "crate"}},
                {"id": "N2", "task_id": "R1_T3", "robot": "puppypi_1", "action": "inspect", "args": {"target": "table"}, "depends_on": ["N1"]}]}
            outcome = MissionOrchestrator(Provider(draft)).orchestrate(proposal["handoff"], self.fleet())
            assert outcome.plan is not None, outcome
            write_json(values[values.index("--dispatch-output") + 1], outcome.plan)
        write_json(values[values.index("--output") + 1], proposal)
        return 0 if proposal["status"] == "ready" else 3


@unittest.skipUnless(EDGE.exists(), "paired edge checkout is needed for mailbox integration")
class RecoveryIntegrationTests(unittest.TestCase):
    def wait_stage(self, console, stage):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = console.state()
            if state["stage"] == stage:
                return state
            if state["status"] != "running":
                self.fail(state["message"])
            time.sleep(.02)
        self.fail(f"did not reach {stage}: {console.state()}")

    def test_clarification_then_automatic_activation_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            console = RecoveryConsole(directory)
            identity = console.submit("Find apple and toy, then inspect table", "ground_truth", recovery=True)
            state = self.wait_stage(console, "clarification")
            console.recover({"job_id": identity, "source_revision": 0, "proposal_id": state["recovery"]["proposal_id"], "action": "clarify", "text": "Try the known crate."})
            console.worker.join(10)
            state = console.state()
            self.assertFalse(console.worker.is_alive(), state)
            self.assertEqual(state["status"], "succeeded", state["message"])
            life = state["results"]["lifecycle"]
            self.assertEqual(life["active_revision"], 1)
            self.assertEqual(life["completed_objective_ids"], ["T1", "T2", "T3"])
            self.assertEqual(len(life["clarifications"]), 1)
            self.assertIsNone(console.actor.error)

    def test_manual_mode_waits_and_rejects_stale_ui_input(self):
        with tempfile.TemporaryDirectory() as directory:
            console = RecoveryConsole(directory, clarification=False)
            identity = console.submit("Find apple and toy, then inspect table", "ground_truth", recovery=True, automatic_recovery=False)
            state = self.wait_stage(console, "review")
            self.assertFalse((Path(directory) / identity / "session/reports/1.json").exists())
            with self.assertRaises(ValueError):
                console.recover({"job_id": identity, "source_revision": 1, "action": "activate"})
            console.recover({"job_id": identity, "source_revision": 0, "proposal_id": state["recovery"]["proposal_id"], "action": "activate"})
            console.worker.join(10)
            self.assertEqual(console.state()["status"], "succeeded", console.state()["message"])

    def test_unchanged_retry_is_held_and_stop_closes_session(self):
        with tempfile.TemporaryDirectory() as directory:
            console = RecoveryConsole(directory, clarification=False, unchanged=True)
            identity = console.submit("Find apple and toy, then inspect table", "ground_truth", recovery=True)
            state = self.wait_stage(console, "review")
            self.assertFalse(state["recovery"]["can_activate"])
            self.assertTrue(any("unchanged" in r for r in state["recovery"]["reasons"]))
            console.stop()
            console.worker.join(5)
            self.assertEqual(console.state()["status"], "stopped")
            self.assertTrue(console.actor.done.is_set())
