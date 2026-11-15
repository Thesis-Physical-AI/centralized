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
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
EDGE = ROOT.parent / "decentralised"
VLM = "http://127.0.0.1:8765"


def read_json(path):
    return json.loads(Path(path).read_text())


def vlm_health():
    try:
        with urllib.request.urlopen(VLM + "/health", timeout=2) as response:
            return {"connected": True, **json.load(response)}
    except (OSError, ValueError):
        return {"connected": False}


class Console:
    def __init__(self, output=None):
        self.output = Path(output or ROOT / "outputs/dashboard")
        self.lock = Lock()
        self.job = None
        self.cancel = False
        self.vlm_process = None
        self.worker = None
        saved = sorted(self.output.glob("*/state.json"))
        if saved:
            try:
                previous = read_json(saved[-1])
                if (previous.get("status") in {"succeeded", "needs_attention", "error", "stopped"}
                        and previous.get("id") == saved[-1].parent.name):
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
        (self.output / snapshot["id"] / "state.json").write_text(json.dumps(snapshot, indent=2))

    def submit(self, mission, perception):
        if not isinstance(mission, str) or not 1 <= len(mission.strip()) <= 5000:
            raise ValueError("Enter a mission between 1 and 5,000 characters.")
        if perception not in {"vlm", "ground_truth"}:
            raise ValueError("Choose VLM camera or ground-truth test perception.")
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise ValueError("A mission is already running. Stop it before starting another.")
            identity = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
            folder = self.output / identity
            folder.mkdir(parents=True)
            self.cancel = False
            self.job = {"id": identity, "mission": mission.strip(), "perception": perception,
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

    def check_cancel(self):
        with self.lock:
            canceled = self.cancel
        if canceled:
            raise InterruptedError("Mission stopped. No replacement plan was activated.")

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
        request = read_json(ROOT / "reasoning-layer/examples/replanning_one_room_request.json")
        with self.lock:
            mission, perception = self.job["mission"], self.job["perception"]
        request.update(mission_id="M-" + identity, natural_language=mission)
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
                self.update(status=status, results=results, message="No simulation was launched. " + reason)
                return
            if code:
                raise RuntimeError("Planning failed; no simulation was launched.")
            results["dispatch"] = read_json(output("dispatch"))
            completed.append("planning")
            phase("perception", "Connecting to the VLM camera server." if perception == "vlm" else "Using ground-truth test perception.")
            if perception == "vlm":
                self.ensure_vlm(folder)
            completed.append("perception")
            phase("execution", "PuppyPi is executing the validated plan in Isaac Sim.")
            simulator = ROOT.parent / "isaacsim/_build/linux-x86_64/release/python.sh"
            command = [simulator, EDGE / "run_prototype.py", "--dispatch", output("dispatch"),
                       "--plan-revision", "0", "--perceiver", perception, "--out", folder / "simulation"]
            if perception == "vlm":
                command.extend(["--approach", "live", "--vlm-url", VLM])
            # A halted mission can exit nonzero while still producing valid feedback.
            self.command(command, EDGE, folder, "simulation", 900)
            report = folder / "simulation/results.json"
            if not report.exists():
                raise RuntimeError("Simulator ended without an execution report. See the run log.")
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
            else:
                self.respond({"error": "Not found"}, 404)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.trusted_host() or origin != "http://" + self.headers.get("Host", ""):
                self.respond({"error": "Use the local dashboard to submit missions"}, 403)
                return
            if self.path not in {"/api/missions", "/api/stop"}:
                self.respond({"error": "Not found"}, 404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 30000:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Request must be an object")
                if self.path == "/api/stop":
                    self.respond(console.stop())
                else:
                    self.respond({"id": console.submit(data.get("mission"), data.get("perception", "vlm"))}, 202)
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
