"""Local mission console: python mission_dashboard.py (http://127.0.0.1:8787)."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
from threading import Lock, Thread
import re
import time
import urllib.request
import uuid

from sim_scene import NoSimRobot, SceneError, generate_scene
from structured_mission_interface import validate_world_context
from mission_lifecycle import MissionLifecycle, SessionClient

ROOT = Path(__file__).resolve().parent
EDGE = ROOT.parent / "decentralised"
VLM = "http://127.0.0.1:8765"
WORLDS = ROOT / "worlds"
FLEET = ROOT / "mission-orchestrator/robots.json"
DEMO_WORLD = "demo_living_room"
# isaac_sim: launch the Isaac Sim prototype; plan_only: stop after dispatch;
# external: wait for any robot/bridge to POST its execution report to /api/report.
BACKENDS = ("isaac_sim", "plan_only", "external")
WORLD_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
FINISHED = {"succeeded", "planned", "needs_attention", "error", "stopped"}
REPORT_WAIT_S = 3600


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    """Write via a temp file so readers never see a half-written document."""
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, indent=2))
    temp.replace(path)


def slug(text):
    value = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:48]
    return value if value and value[0].isalnum() else "world"


def text_field(data, key, limit, required=False):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise ValueError(f"'{key}' must be text of at most {limit} characters" + (" and not empty." if required else "."))
    return value.strip()


def vlm_health():
    try:
        with urllib.request.urlopen(VLM + "/health", timeout=2) as response:
            return {"connected": True, **json.load(response)}
    except (OSError, ValueError):
        return {"connected": False}


class Console:
    def __init__(self, output=None, worlds=None):
        self.output = Path(output or ROOT / "outputs/dashboard")
        self.worlds = Path(worlds or WORLDS)
        self.lock = Lock()
        self.world_lock = Lock()
        self.job = None
        self.cancel = False
        self.vlm_process = None
        self.worker = None
        self.recovery_action = None
        saved = sorted(self.output.glob("*/state.json"))
        if saved:
            try:
                previous = read_json(saved[-1])
                if previous.get("status") in FINISHED and previous.get("id") == saved[-1].parent.name:
                    if previous.get("message") == "Mission stopped. No replacement plan was activated.":
                        previous["message"] = "Mission stopped. No further plan revisions will be activated."
                    self.job = previous
            except (OSError, ValueError, AttributeError):
                pass

    def state(self):
        with self.lock:
            job = deepcopy(self.job)
        if job:
            folder = self.output / job["id"]
            job["log"] = "\n".join(p.read_text(errors="replace")[-5000:]
                                    for p in sorted(folder.glob("*.log"))) [-10000:]
            events = folder / "simulation/events.jsonl"
            job["events"] = []
            if events.exists():
                for line in events.read_text(errors="replace").splitlines()[-20:]:
                    try:
                        job["events"].append(json.loads(line))
                    except ValueError:
                        pass  # last line can still be in flight
        return job

    def update(self, **values):
        with self.lock:
            self.job.update(values)
            snapshot = deepcopy(self.job)
        write_json(self.output / snapshot["id"] / "state.json", snapshot)

    # ---- worlds: the demo ships read-only; anything created in the dashboard sits next to it.

    @staticmethod
    def fleet():
        return read_json(FLEET)

    def describe(self, spec):
        # The Isaac Sim scene is generated from the world every time, never stored.
        overrides = spec.get("sim") or {}
        try:
            scene, issue = generate_scene(spec["world"], overrides), ""
        except (SceneError, KeyError, TypeError, AttributeError) as error:
            scene, issue = None, str(error)
        return {"id": spec["id"], "name": spec.get("name") or spec["id"], "description": spec.get("description", ""),
                "notes": spec.get("notes", ""), "demo": bool(spec.get("demo")), "samples": spec.get("samples", []),
                "world": spec["world"], "sim": overrides, "sim_scene": scene, "sim_issue": issue,
                "backends": [b for b in BACKENDS if scene or b != "isaac_sim"]}

    def list_worlds(self):
        items = []
        for path in sorted(self.worlds.glob("*.json")):
            try:
                spec = read_json(path)
            except (OSError, ValueError):
                continue
            if isinstance(spec, dict) and spec.get("id") == path.stem and isinstance(spec.get("world"), dict):
                items.append(self.describe(spec))
        return sorted(items, key=lambda w: (not w["demo"], w["name"].lower()))

    def get_world(self, world_id):
        if not isinstance(world_id, str) or not WORLD_ID.match(world_id):
            raise ValueError("Choose a world.")
        try:
            spec = read_json(self.worlds / f"{world_id}.json")
        except (OSError, ValueError):
            raise ValueError(f"World '{world_id}' was not found.") from None
        if spec.get("id") != world_id or not isinstance(spec.get("world"), dict):
            raise ValueError(f"World '{world_id}' is malformed.")
        return self.describe(spec)

    def save_world(self, data):
        name = text_field(data, "name", 80, required=True)
        spec = {"name": name, "description": text_field(data, "description", 200),
                "notes": text_field(data, "notes", 1000)}
        samples = data.get("samples", [])
        if (not isinstance(samples, list) or len(samples) > 12
                or not all(isinstance(s, str) and 0 < len(s.strip()) <= 5000 for s in samples)):
            raise ValueError("Sample missions must be up to 12 non-empty lines.")
        spec["samples"] = [s.strip() for s in samples]
        world = data.get("world")
        if not isinstance(world, dict):
            raise ValueError("The world description must be a JSON object.")
        result = validate_world_context(world, self.fleet())
        if not result.valid:
            raise ValueError("World is not valid: " + "; ".join(
                f"{e.path.removeprefix('/world')}: {e.message}" for e in result.errors[:6]))
        spec["world"] = world
        if "sim" in data:  # layout overrides, e.g. carried over when duplicating the demo
            overrides = data["sim"] or {}
            if isinstance(overrides, dict) and isinstance(overrides.get("entities"), dict):
                overrides = {**overrides, "entities": {k: v for k, v in overrides["entities"].items()
                                                       if k in world["entities"]}}
            try:
                generate_scene(world, overrides)
            except NoSimRobot:
                pass
            except (SceneError, TypeError, AttributeError) as error:
                raise ValueError(f"Sim layout is not valid: {error}") from None
            spec["sim"] = overrides
        with self.world_lock:
            world_id = data.get("id")
            if world_id:  # update an existing world
                existing = self.get_world(world_id)
                if existing["demo"]:
                    raise ValueError("The demo world is read-only. Duplicate it to make changes.")
                if "sim" not in spec and existing["sim"]:
                    spec["sim"] = existing["sim"]
            else:
                base = world_id = slug(name)
                n = 2
                while (self.worlds / f"{world_id}.json").exists():
                    world_id, n = f"{base}_{n}", n + 1
            spec = {"id": world_id, **spec}
            self.worlds.mkdir(parents=True, exist_ok=True)
            write_json(self.worlds / f"{world_id}.json", spec)
        return self.describe(spec)

    def delete_world(self, world_id):
        with self.world_lock:
            if self.get_world(world_id)["demo"]:
                raise ValueError("The demo world cannot be deleted.")
            (self.worlds / f"{world_id}.json").unlink()
        return {"ok": True}

    # ---- missions

    def submit(self, mission, perception, world_id=DEMO_WORLD, backend="isaac_sim", *, recovery=False, automatic_recovery=True):
        if not isinstance(mission, str) or not 1 <= len(mission.strip()) <= 5000:
            raise ValueError("Enter a mission between 1 and 5,000 characters.")
        if perception not in {"vlm", "ground_truth"}:
            raise ValueError("Choose VLM camera or ground-truth test perception.")
        if backend not in BACKENDS:
            raise ValueError("Choose Isaac Sim, plan only or external robot execution.")
        if type(recovery) is not bool or type(automatic_recovery) is not bool:
            raise ValueError("Recovery settings must be booleans")
        if recovery and backend != "isaac_sim":
            raise ValueError("Revision activation is currently supported by the Isaac Sim session backend only")
        world = self.get_world(world_id)
        if backend not in world["backends"]:
            raise ValueError(f"'{world['name']}' cannot run in Isaac Sim: {world['sim_issue']}. "
                             "Choose plan only or external robot.")
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise ValueError("A mission is already running. Stop it before starting another.")
            identity = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
            folder = self.output / identity
            folder.mkdir(parents=True)
            self.cancel = False
            self.recovery_action = None
            # The world is snapshotted, so editing it mid-run never changes what this run planned against.
            self.job = {"id": identity, "mission": mission.strip(), "perception": perception,
                        "backend": backend, "world": world,
                        "recovery_enabled": recovery, "automatic_recovery": automatic_recovery, "active_revision": 0,
                        "status": "running", "stage": "planning", "completed": [], "results": {},
                        "message": "Gemini is decomposing and allocating the mission.",
                        "started_at": datetime.now(timezone.utc).isoformat()}
        self.worker = Thread(target=self.run, args=(identity,), daemon=True)
        self.worker.start()
        return identity

    def stop(self):
        with self.lock:
            self.cancel = True
        return {"ok": True}

    def recover(self, data):
        """Queue operator input for the current paused revision, never execute in HTTP threads."""
        with self.lock:
            job = self.job
            if (not job or job["id"] != data.get("job_id") or job["status"] != "running"
                    or not job.get("recovery_enabled") or job["stage"] not in {"clarification", "review"}
                    or type(data.get("source_revision")) is not int
                    or data["source_revision"] != job["active_revision"]
                    or data.get("proposal_id") != job.get("recovery", {}).get("proposal_id")):
                raise ValueError("This mission/revision is not waiting for recovery input")
            if self.recovery_action is not None:
                raise ValueError("Recovery input is already being processed")
            action = data.get("action")
            if action == "clarify":
                text = text_field(data, "text", 5000, required=True)
                self.recovery_action = {"action": action, "text": text}
            elif action == "activate" and job["stage"] == "review":
                if type(data.get("acknowledge_review", False)) is not bool:
                    raise ValueError("Review confirmation must be a boolean")
                self.recovery_action = {"action": action, "acknowledge_review": data.get("acknowledge_review", False)}
            else:
                raise ValueError("Choose clarification or activation of the current proposal")
        return {"ok": True}

    def wait_recovery_action(self, client):
        deadline = time.monotonic() + 3500
        while time.monotonic() < deadline:
            self.check_cancel()
            if not client.alive():
                raise RuntimeError("The paused simulator session ended. It cannot be resumed by resetting the scene.")
            with self.lock:
                action, self.recovery_action = self.recovery_action, None
            if action:
                return action
            time.sleep(.2)
        raise TimeoutError("Recovery input timed out; the session was stopped")

    def start_simulator(self, command, folder):
        with (folder / "simulation.log").open("w") as stdout, (folder / "simulation-errors.log").open("w") as stderr:
            return subprocess.Popen([str(a) for a in command], cwd=EDGE, stdout=stdout, stderr=stderr,
                                    start_new_session=True)

    @staticmethod
    def terminate_process(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def run_persistent(self, command, folder, request, scene, results, completed):
        """One physical session, bounded revisions, and explicit clarification waits."""
        candidate = read_json(folder / "candidate.json") if (folder / "candidate.json").exists() else {}
        life = MissionLifecycle(folder, request, results["dispatch"], self.fleet(), objectives=candidate.get("tasks"),
                                execution_mode="live" if self.job["perception"] == "vlm" else "map")
        command = [*command, "--session-dir", folder / "session"]
        process = self.start_simulator(command, folder)
        client = SessionClient(folder / "session", check_cancel=self.check_cancel, alive=lambda: process.poll() is None)
        proposal_index = 0

        def publish(stage, message, **extra):
            self.check_cancel()
            results["lifecycle"] = deepcopy(life.data)
            self.update(stage=stage, message=message, completed=completed[:], results=deepcopy(results),
                        active_revision=life.data["active_revision"], **extra)

        def close_session():
            state = client.checkpoint()
            if state["phase"] == "IDLE":
                client.send({"command_id": uuid.uuid4().hex, "action": "close", "mission_id": state["mission_id"],
                             "session_id": state["session_id"], "expected_revision": state["active_revision"],
                             "quiescence_token": state["quiescence_token"]})
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass

        try:
            while True:
                revision = life.data["active_revision"]
                report = client.wait(f"reports/{revision}.json")
                results["execution"] = report
                if "execution" not in completed:
                    completed.append("execution")
                publish("feedback", f"Ingesting revision {revision} feedback from the same simulator session.")
                feedback = life.record_report(report)
                results["feedback"] = feedback
                write_json(folder / f"feedback-r{revision}.json", feedback)
                if "feedback" not in completed:
                    completed.append("feedback")
                if feedback["status"] == "SUCCEEDED":
                    close_session()
                    publish("feedback", "All remaining objectives completed in the same simulator session.", recovery=None)
                    return "succeeded", "Mission completed; revision history and feedback are saved."
                if feedback["status"] != "HALTED":
                    close_session()
                    return "needs_attention", "The session is pending on unsupported peers/dependencies. No work was discarded."
                reconciliation_error = ""
                try:
                    life.reconcile_pose(scene)
                except ValueError as error:
                    reconciliation_error = str(error)
                if revision >= life.data["max_revisions"]:
                    close_session()
                    return "needs_attention", "Recovery revision limit reached. Review the mission before any further execution."
                while True:
                    proposal_index += 1
                    artifact = folder / "revisions" / f"{revision}-proposal-{proposal_index}"
                    artifact.mkdir(parents=True)
                    write_json(artifact / "request.json", life.current["request"])
                    write_json(artifact / "dispatch.json", life.current["dispatch"])
                    write_json(artifact / "report.json", life.current["report"])
                    write_json(artifact / "history.json", life.history())
                    text = "\n".join(item["text"] for item in life.data["clarifications"])
                    write_json(artifact / "clarification.json", {"text": text[-5000:]})
                    publish("replanning", f"Planning recovery for revision {revision + 1}; the robot is paused.", recovery=None)
                    code = self.command([ROOT / ".venv/bin/python", ROOT / "reasoning-layer/replan_mission.py",
                        "--input", artifact / "request.json", "--dispatch", artifact / "dispatch.json",
                        "--report", artifact / "report.json", "--history", artifact / "history.json",
                        "--clarification", artifact / "clarification.json", "--plan-revision", str(revision),
                        "--output", artifact / "proposal.json", "--dispatch-output", artifact / "replacement.json"],
                        ROOT, folder, f"replanning-r{revision}-{proposal_index}", 130)
                    if code not in {0, 3} or not (artifact / "proposal.json").exists():
                        raise RuntimeError("Recovery generation failed; the paused session was stopped. See the run log.")
                    proposal = read_json(artifact / "proposal.json")
                    results["replan"] = proposal
                    if "replanning" not in completed:
                        completed.append("replanning")
                    reasons = []
                    life.data["pending"] = None
                    life.data["status"] = "WAITING_REPLAN"
                    life.save()
                    if proposal["status"] == "ready" and (artifact / "replacement.json").exists():
                        try:
                            reasons = life.offer(proposal, read_json(artifact / "replacement.json"))
                        except ValueError as error:
                            reasons = [str(error)]
                    else:
                        reasons = ["Clarification or a valid orchestrated replacement is required."]
                        if proposal.get("orchestration"):
                            reasons.extend(proposal["orchestration"].get("errors", []))
                            reasons.extend(proposal["orchestration"].get("warnings", []))
                    if reconciliation_error:
                        reasons.append(reconciliation_error)
                    life.data["recovery_checks"] = reasons
                    life.save()
                    with self.lock:
                        automatic = self.job["automatic_recovery"]
                    action = {"action": "activate", "acknowledge_review": False} if automatic and not reasons else None
                    while action is None:
                        ready = life.data["pending"] is not None
                        publish("review" if ready else "clarification",
                                "Robot paused. Review recovery gates or provide more information.",
                                recovery={"revision": revision, "can_activate": ready and not reconciliation_error
                                          and not life.activation_reasons(acknowledge_review=True),
                                          "proposal_id": artifact.name,
                                          "reasons": reasons, "requires_answer": not ready})
                        action = self.wait_recovery_action(client)
                        if action["action"] == "clarify":
                            life.add_clarification(action["text"])
                            break
                        blocked = life.activation_reasons(acknowledge_review=action["acknowledge_review"])
                        if blocked or reconciliation_error:
                            reasons = blocked + ([reconciliation_error] if reconciliation_error else [])
                            action = None
                    if action["action"] == "clarify":
                        continue
                    publish("activation", "Confirming the old revision is idle before activating the replacement.", recovery=None)
                    state = client.checkpoint()
                    pause = {"command_id": uuid.uuid4().hex, "action": "pause", "mission_id": state["mission_id"],
                             "session_id": state["session_id"], "expected_revision": revision,
                             "quiescence_token": state["quiescence_token"]}
                    pause_ack = client.send(pause)
                    write_json(artifact / "pause-ack.json", pause_ack)
                    activation = life.begin_activation(client.checkpoint(), pause_ack=pause_ack,
                                                       acknowledge_review=action["acknowledge_review"])
                    ack = client.send(activation)
                    life.commit_activation(ack)
                    results["dispatch"] = deepcopy(life.current["dispatch"])
                    results.pop("execution", None)
                    results.pop("feedback", None)
                    completed[:] = ["planning", "perception"]
                    publish("execution", f"Revision {life.data['active_revision']} activated; continuing without resetting the scene.")
                    break
        finally:
            self.terminate_process(process)

    def receive_report(self, job_id, report):
        """Accept an execution report from any robot or bridge for the run waiting on one."""
        with self.lock:
            job = self.job
            waiting = (job and job["id"] == job_id and job["status"] == "running"
                       and job.get("backend") == "external" and job.get("stage") == "execution")
        if not waiting:
            raise ValueError("No mission is waiting for an execution report.")
        if not isinstance(report, dict) or not isinstance(report.get("execution"), dict) \
                or not isinstance(report["execution"].get("steps"), list):
            raise ValueError("The report must be an object with execution.steps (see DASHBOARD.md).")
        if report.get("mission_id") != "M-" + job_id:
            raise ValueError(f"The report's mission_id must be 'M-{job_id}'.")
        target = self.output / job_id / "simulation"
        target.mkdir(exist_ok=True)
        write_json(target / "results.json", report)
        return {"ok": True}

    def wait_for_report(self, report):
        deadline = time.monotonic() + REPORT_WAIT_S
        while not report.exists():
            self.check_cancel()
            if time.monotonic() > deadline:
                raise TimeoutError("No execution report arrived within an hour.")
            time.sleep(0.5)

    def check_cancel(self):
        with self.lock:
            canceled = self.cancel
        if canceled:
            raise InterruptedError("Mission stopped. No further plan revisions will be activated.")

    def command(self, args, cwd, folder, name, timeout):
        self.check_cancel()
        with (folder / f"{name}.log").open("w") as stdout, (folder / f"{name}-errors.log").open("w") as stderr:
            process = subprocess.Popen([str(a) for a in args], cwd=cwd, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    self.check_cancel()
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{name} exceeded its time limit.")
                    time.sleep(0.2)
                return process.returncode
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()

    def ensure_vlm(self, folder):
        if vlm_health()["connected"]:
            return
        # Reuse our server if an earlier mission was canceled during model loading.
        if self.vlm_process is None or self.vlm_process.poll() is not None:
            with (folder / "vlm-startup.log").open("w") as log:
                self.vlm_process = subprocess.Popen(
                    [str(EDGE / ".venv/bin/python"), "vlm_server/smolvlm_server.py", "--model",
                     "models/SmolVLM-Instruct", "--profile", "workstation_gpu_live"],
                    cwd=EDGE, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            self.check_cancel()
            if vlm_health()["connected"]:
                return
            if self.vlm_process.poll() is not None:
                raise RuntimeError("The VLM server could not start. See the run log.")
            time.sleep(1)
        raise TimeoutError("The VLM server did not become ready within 150 seconds.")

    def run(self, identity):
        folder = self.output / identity
        python = ROOT / ".venv/bin/python"
        with self.lock:
            mission, perception = self.job["mission"], self.job["perception"]
            backend, world = self.job["backend"], deepcopy(self.job["world"])
        request = {"mission_id": "M-" + identity, "natural_language": mission, "world": world["world"]}
        (folder / "request.json").write_text(json.dumps(request, indent=2))
        completed, results = [], {}

        def phase(name, message):
            self.check_cancel()
            self.update(stage=name, message=message, completed=completed[:], results=deepcopy(results))

        def output(name):
            return folder / (name + ".json")

        try:
            code = self.command([python, ROOT / "reasoning-layer/run_mission.py", "--input", output("request"),
                                 "--handoff-output", output("candidate"), "--output", output("dispatch")],
                                ROOT, folder, "planning", 130)
            if not output("dispatch").exists():
                try:
                    results["decision"] = read_json(folder / "planning.log")
                except (ValueError, OSError):
                    pass
                status = "needs_attention" if code == 3 else "error"
                errors = folder / "planning-errors.log"
                details = errors.read_text(errors="replace") if errors.exists() else ""
                reason = next((line.removeprefix("ERROR: ") for line in details.splitlines()
                               if line.startswith("ERROR:")), "Review the planner response or run log.")
                self.update(status=status, results=results, message="No robot was dispatched. " + reason)
                return
            if code:
                raise RuntimeError("Planning failed; no robot was dispatched.")
            results["dispatch"] = read_json(output("dispatch"))
            completed.append("planning")
            if backend == "plan_only":
                self.update(status="planned", results=results, completed=completed,
                            message="Plan ready. Plan-only mode: no robot was dispatched.")
                return
            report = folder / "simulation/results.json"
            if backend == "isaac_sim":
                phase("perception", "Connecting to the VLM camera server." if perception == "vlm" else "Using ground-truth test perception.")
                if perception == "vlm":
                    self.ensure_vlm(folder)
                completed.append("perception")
                # Generate the sim scene from the run's world snapshot; the simulator reads only this file.
                write_json(folder / "sim_world.json", generate_scene(world["world"], world["sim"]))
                phase("execution", "The robot is executing the validated plan in Isaac Sim.")
                simulator = ROOT.parent / "isaacsim/_build/linux-x86_64/release/python.sh"
                command = [simulator, EDGE / "run_prototype.py", "--dispatch", output("dispatch"),
                           "--world", folder / "sim_world.json",
                           "--plan-revision", "0", "--perceiver", perception, "--out", folder / "simulation"]
                if perception == "vlm":
                    command.extend(["--approach", "live", "--vlm-url", VLM])
                if self.job.get("recovery_enabled"):
                    status, message = self.run_persistent(command, folder, request, read_json(folder / "sim_world.json"), results, completed)
                    self.update(status=status, message=message, results=results, completed=completed, recovery=None)
                    return
                # A halted mission can exit nonzero while still producing valid feedback.
                self.command(command, EDGE, folder, "simulation", 900)
                if not report.exists():
                    raise RuntimeError("Simulator ended without an execution report. See the run log.")
            else:
                phase("execution", "Plan dispatched. Waiting for the robot to send its execution report.")
                self.wait_for_report(report)
            results["execution"] = read_json(report)
            completed.append("execution")
            phase("feedback", "Validating results and updating the central world snapshot.")
            code = self.command([python, "-m", "structured_mission_interface.feedback", "--dispatch", output("dispatch"),
                                 "--report", report, "--plan-revision", "0", "--output", output("feedback")],
                                ROOT, folder, "feedback", 30)
            if code:
                raise RuntimeError("Feedback validation failed. See the run log.")
            results["feedback"] = read_json(output("feedback"))
            completed.append("feedback")
            if results["feedback"]["status"] == "HALTED":
                phase("replanning", "Gemini is evaluating the failure and remaining work.")
                code = self.command([python, ROOT / "reasoning-layer/replan_mission.py", "--input", output("request"),
                                     "--dispatch", output("dispatch"), "--report", report, "--plan-revision", "0",
                                     "--output", output("replan")], ROOT, folder, "replanning", 75)
                if code not in {0, 3} or not output("replan").exists():
                    raise RuntimeError("Replanning failed. See the run log.")
                results["replan"] = read_json(output("replan"))
                completed.append("replanning")
                status, message = "needs_attention", "Execution halted. Review Gemini’s replanning proposal below."
            elif results["feedback"]["status"] == "SUCCEEDED":
                status, message = "succeeded", "All dispatched steps succeeded. Feedback has been ingested."
            else:
                status, message = "needs_attention", "Execution is pending on unavailable robots or dependencies."
            self.update(status=status, results=results, completed=completed, message=message)
        except InterruptedError as error:
            self.update(status="stopped", message=str(error), results=results, completed=completed)
        except Exception as error:
            self.update(status="error", message=str(error), results=results, completed=completed)


def make_handler(console):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, value, code=200, content_type="application/json"):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def trusted_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.trusted_host():
                self.respond({"error": "Local access only"}, 403)
            elif self.path == "/":
                self.respond((ROOT / "dashboard.html").read_bytes(), content_type="text/html; charset=utf-8")
            elif self.path == "/api/state":
                self.respond({"job": console.state()})
            elif self.path == "/api/vlm":
                self.respond(vlm_health())
            elif self.path == "/api/worlds":
                fleet = console.fleet()
                self.respond({"worlds": console.list_worlds(), "default": DEMO_WORLD,
                              "robots": [{"id": r["id"], "role": r.get("role", "")} for r in fleet["robots"]]})
            else:
                self.respond({"error": "Not found"}, 404)

        # Request size limits per endpoint; execution reports carry perception traces, so allow more.
        LIMITS = {"/api/missions": 30000, "/api/stop": 30000, "/api/worlds": 200000,
                  "/api/worlds/delete": 30000, "/api/report": 5_000_000, "/api/recovery": 30000}

        def do_POST(self):
            origin, local = self.headers.get("Origin"), "http://" + self.headers.get("Host", "")
            # Browsers always send Origin on POST, so a missing one is a local script (robot bridge, curl);
            # only /api/report accepts that. A foreign Origin is always refused.
            if not self.trusted_host() or not (origin == local or (origin is None and self.path == "/api/report")):
                self.respond({"error": "Use the local dashboard to submit missions"}, 403)
                return
            if self.path not in self.LIMITS:
                self.respond({"error": "Not found"}, 404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= self.LIMITS[self.path]:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Request must be an object")
                if self.path == "/api/stop":
                    self.respond(console.stop())
                elif self.path == "/api/worlds":
                    self.respond({"world": console.save_world(data)})
                elif self.path == "/api/worlds/delete":
                    self.respond(console.delete_world(data.get("id")))
                elif self.path == "/api/report":
                    self.respond(console.receive_report(data.get("job_id"), data.get("report")))
                elif self.path == "/api/recovery":
                    self.respond(console.recover(data))
                else:
                    self.respond({"id": console.submit(data.get("mission"), data.get("perception", "vlm"),
                                                       data.get("world", DEMO_WORLD),
                                                       data.get("backend", "isaac_sim"),
                                                       recovery=data.get("recovery", False),
                                                       automatic_recovery=data.get("automatic_recovery", True))}, 202)
            except (ValueError, TypeError) as error:
                self.respond({"error": str(error)}, 400)

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    console = Console()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(console))
    print(f"Mission dashboard: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        console.stop()
        if console.worker:
            console.worker.join(timeout=12)
        server.server_close()


if __name__ == "__main__":
    main()
