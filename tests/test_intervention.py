import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

import babysit

SESSION = {"HERMES_SESSION_KEY": "intervention-session", "HERMES_SESSION_ID": "intervention-id"}


class Result:
    ok = True
    error = None

    def edit(self, text):
        return {"ok": True}


class Context:
    plugin_id = "babysit"

    def __init__(self):
        self.injections = []

    def post_message(self, text):
        return Result()

    def inject_message(self, text, *, session_key):
        self.injections.append((text, session_key))
        return True

    def session_surface(self):
        return "telegram"


class InterventionTests(unittest.TestCase):
    def setUp(self):
        babysit._WATCHES.clear()
        babysit._HISTORY.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "job.log"
        self.log.touch()
        gateway = types.ModuleType("gateway")
        session_context = types.ModuleType("gateway.session_context")
        session_context.get_session_env = lambda key, default="": SESSION.get(key, default)
        self.modules = patch.dict(sys.modules, {"gateway": gateway, "gateway.session_context": session_context})
        self.modules.start()
        self.poll = patch("babysit._POLL_SECONDS", 0.02)
        self.poll.start()
        self.ctx = Context()

    def tearDown(self):
        babysit._cleanup()
        for state in list(babysit._WATCHES.values()):
            state["thread"].join(1)
        self.modules.stop()
        self.poll.stop()
        self.temp.cleanup()

    def child(self):
        code = "import time; n=0\nwhile True:\n n+=1\n time.sleep(.03)"
        proc = subprocess.Popen([sys.executable, "-u", "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_child, proc)
        return proc

    @staticmethod
    def stop_child(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)

    def watch(self, **args):
        result = json.loads(babysit._watch({"log_path": str(self.log), **args}, ctx=self.ctx))
        self.assertTrue(result["ok"], result)
        return result["watch_id"]

    def wait(self, predicate, timeout=3):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return True
            time.sleep(.01)
        return bool(predicate())

    def emit_nan(self, step=7):
        with self.log.open("a") as out:
            out.write(f"step {step}/20 loss=nan\n")

    def test_real_child_is_stopped_on_nan_and_resumed_by_tool(self):
        child = self.child()
        watch_id = self.watch(pid=child.pid, on_failure="pause")
        self.emit_nan()
        self.assertTrue(self.wait(lambda: psutil.Process(child.pid).status() == psutil.STATUS_STOPPED))
        self.assertIn("[babysit] PAUSED job.log at step 7 (nan)", self.ctx.injections[0][0])
        self.assertEqual(json.loads(babysit._resume({"watch_id": watch_id}))["outcome"], "process resumed")
        self.assertTrue(self.wait(lambda: psutil.Process(child.pid).status() != psutil.STATUS_STOPPED))

    def test_other_session_cannot_resume_and_default_wake_never_stops(self):
        child = self.child()
        watch_id = self.watch(pid=child.pid)
        self.emit_nan()
        self.assertTrue(self.wait(lambda: bool(self.ctx.injections)))
        self.assertNotEqual(psutil.Process(child.pid).status(), psutil.STATUS_STOPPED)
        result = json.loads(babysit._resume({"watch_id": watch_id}, session_key="different-session"))
        self.assertFalse(result["ok"])
        self.assertNotEqual(psutil.Process(child.pid).status(), psutil.STATUS_STOPPED)

    def test_other_session_cannot_resume_a_paused_child(self):
        child = self.child()
        watch_id = self.watch(pid=child.pid, on_failure="pause")
        self.emit_nan()
        self.assertTrue(self.wait(lambda: psutil.Process(child.pid).status() == psutil.STATUS_STOPPED))
        result = json.loads(babysit._resume({"watch_id": watch_id}, session_key="different-session"))
        self.assertFalse(result["ok"])
        self.assertEqual(psutil.Process(child.pid).status(), psutil.STATUS_STOPPED)

    def test_pause_requires_pid_and_same_owner(self):
        result = json.loads(babysit._watch({"log_path": str(self.log), "on_failure": "pause"}, ctx=self.ctx))
        self.assertFalse(result["ok"])
        self.assertIn("requires pid", result["error"])
        child = self.child()
        with patch("babysit.psutil.Process.uids", return_value=types.SimpleNamespace(real=os.getuid() + 1)):
            result = json.loads(babysit._watch({"log_path": str(self.log), "pid": child.pid, "on_failure": "pause"}, ctx=self.ctx))
        self.assertFalse(result["ok"])
        self.assertIn("Hermes uid", result["error"])

    def test_pause_refuses_hermes_own_process_and_parent(self):
        for pid in (os.getpid(), os.getppid()):
            with patch("babysit.os.kill") as kill:
                result = json.loads(babysit._watch({"log_path": str(self.log), "pid": pid, "on_failure": "pause"}, ctx=self.ctx))
            self.assertFalse(result["ok"], pid)
            self.assertIn("own process", result["error"])
            kill.assert_not_called()

    def test_identity_mismatch_never_signals_and_says_pid_was_reused(self):
        child = self.child()
        watch_id = self.watch(pid=child.pid, on_failure="pause")
        state = babysit._WATCHES[watch_id]
        state["create_time"] += 10
        self.emit_nan()
        self.assertTrue(self.wait(lambda: bool(self.ctx.injections)))
        self.assertNotEqual(psutil.Process(child.pid).status(), psutil.STATUS_STOPPED)
        self.assertIn("pid was reused; not paused", self.ctx.injections[0][0])

    def test_recorded_uid_mismatch_never_signals(self):
        child = self.child()
        watch_id = self.watch(pid=child.pid, on_failure="pause")
        babysit._WATCHES[watch_id]["uid"] = os.getuid() + 1
        self.emit_nan()
        self.assertTrue(self.wait(lambda: bool(self.ctx.injections)))
        self.assertNotEqual(psutil.Process(child.pid).status(), psutil.STATUS_STOPPED)
        self.assertIn("pid was reused; not paused", self.ctx.injections[0][0])

    def test_slash_command_resumes_a_paused_child(self):
        child = self.child()
        watch_id = self.watch(pid=child.pid, on_failure="pause")
        self.emit_nan()
        self.assertTrue(self.wait(lambda: psutil.Process(child.pid).status() == psutil.STATUS_STOPPED))
        self.assertEqual(babysit._command(f"resume {watch_id}", session_key=SESSION["HERMES_SESSION_KEY"]), "process resumed")
        self.assertTrue(self.wait(lambda: psutil.Process(child.pid).status() != psutil.STATUS_STOPPED))

    def test_resume_rechecks_process_identity_before_sigcont(self):
        child = self.child()
        watch_id = self.watch(pid=child.pid, on_failure="pause")
        self.emit_nan()
        self.assertTrue(self.wait(lambda: psutil.Process(child.pid).status() == psutil.STATUS_STOPPED))
        babysit._WATCHES[watch_id]["create_time"] += 10
        result = json.loads(babysit._resume({"watch_id": watch_id}))
        self.assertFalse(result["ok"])
        self.assertEqual(psutil.Process(child.pid).status(), psutil.STATUS_STOPPED)

    def test_pause_cap_and_resume_slash_command(self):
        child = self.child()
        watch_id = self.watch(pid=child.pid, on_failure="pause")
        state = babysit._WATCHES[watch_id]
        for _ in range(3):
            self.assertEqual(babysit._pause(state, "nan"), "")
            self.assertEqual(json.loads(babysit._resume({"watch_id": watch_id}))["ok"], True)
        self.assertIn("pause limit reached", babysit._pause(state, "nan"))
        self.assertEqual(babysit._command(f"resume {watch_id}", session_key=SESSION["HERMES_SESSION_KEY"]), "watch is not paused")

    def test_recovered_verdict_and_paused_stall_suppression(self):
        child = self.child()
        watch_id = self.watch(pid=child.pid, on_failure="pause")
        state = babysit._WATCHES[watch_id]
        state["predecessor"] = {"failure_step": 6, "failure_kind": "nan"}
        state["resume_of"] = "prior"
        state["attempt"] = 2
        state["lineage"] = [{"attempt": 1, "failure_step": 6, "failure_kind": "nan"},
                            {"attempt": 2, "failure_step": None, "failure_kind": None, "verdict": None}]
        state["metrics"].feed("step 7/20 loss=0.4", time.monotonic())
        babysit._lineage_progress(state, "step 7/20 loss=0.4")
        self.assertEqual(state["verdict"]["kind"], "recovered")
        self.assertTrue(self.wait(lambda: any("passed step 6" in text for text, _ in self.ctx.injections)))
        state["verdict"] = None
        state["metrics"].feed("step 6/20 loss=0.3", time.monotonic())
        babysit._lineage_progress(state, "step 6/20 loss=0.3")
        self.assertIsNone(state["verdict"], "the predecessor's failure step itself is not a recovery")
        state["metrics"].mark("nan", "loss=nan", time.monotonic())
        state["metrics"].feed("step 8/20 loss=0.2", time.monotonic())
        babysit._lineage_progress(state, "step 8/20 loss=0.2")
        self.assertIsNone(state["verdict"], "a non-finite metric since this attempt started blocks recovery")
        self.assertEqual(babysit._pause(state, "nan"), "")
        state["job"].last_line_time = time.monotonic() - state["job"].stall_seconds - 1
        time.sleep(.1)
        self.assertFalse(any(event[2] == "stall" for event in state["metrics"].events))

    def test_checkpoint_reporting_and_protected_or_symlinked_escape_refusal(self):
        check_dir = self.root / "checkpoints"
        check_dir.mkdir()
        older = check_dir / "ckpt_step10.npz"
        newer = check_dir / "ckpt_step20.npz"
        older.write_bytes(b"old")
        time.sleep(.02)
        newer.write_bytes(b"newer")
        self.assertIn("ckpt_step20.npz, 5 bytes", babysit._checkpoint(str(check_dir)))
        self.watch(checkpoint_dir=str(check_dir))
        self.emit_nan()
        self.assertTrue(self.wait(lambda: bool(self.ctx.injections)))
        self.assertIn("Checkpoint: ckpt_step20.npz", self.ctx.injections[0][0])
        result = json.loads(babysit._watch({"log_path": str(self.log), "checkpoint_dir": "/etc"}, ctx=self.ctx))
        self.assertFalse(result["ok"])
        self.assertIn("protected", result["error"])
        escaped = self.root / "escape"
        escaped.symlink_to("/etc", target_is_directory=True)
        result = json.loads(babysit._watch({"log_path": str(self.log), "checkpoint_dir": str(escaped)}, ctx=self.ctx))
        self.assertFalse(result["ok"])

    def test_resume_of_other_session_is_refused_and_lineage_recovery_can_fail(self):
        child = self.child()
        first = self.watch(pid=child.pid)
        prior = babysit._WATCHES[first]
        prior.update(failure_kind="nan", failure_step=6, ended="stopped")
        prior["lineage"][-1].update(failure_kind="nan", failure_step=6)
        babysit._WATCHES.pop(first)
        babysit._HISTORY.setdefault(prior["session_history_key"], __import__("collections").deque(maxlen=8)).append({
            "watch_id": first, "name": prior["name"], "failure_kind": "nan", "failure_step": 6,
            "last_good_step": 5, "last_good_loss": .5, "ended": "stopped", "lineage": prior["lineage"]})
        result = json.loads(babysit._watch({"log_path": str(self.log), "resume_of": first}, ctx=self.ctx))
        self.assertTrue(result["ok"], result)
        second = babysit._WATCHES[result["watch_id"]]
        self.assertEqual(second["attempt"], 2)
        with patch("babysit._caller", return_value=("different-session", "other")):
            refused = json.loads(babysit._watch({"log_path": str(self.log), "resume_of": first}, ctx=self.ctx))
        self.assertFalse(refused["ok"])
        babysit._HISTORY[prior["session_history_key"]][-1]["lineage"] = [
            {"attempt": i, "watch_id": f"{i:012x}", "failure_step": 6, "failure_kind": "nan", "verdict": None}
            for i in range(1, 6)]
        capped = json.loads(babysit._watch({"log_path": str(self.log), "resume_of": first}, ctx=self.ctx))
        self.assertFalse(capped["ok"])
        self.assertIn("5 attempts in this lineage", capped["error"])
        second["metrics"].feed("step 4/20 loss=nan", time.monotonic())
        babysit._handle_event(second, "nan", "step 4 loss=nan", time.monotonic())
        self.assertEqual(second["verdict"]["kind"], "relapsed")


if __name__ == "__main__":
    unittest.main()
