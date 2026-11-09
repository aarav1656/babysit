import json
import tempfile
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from babysit import _WATCHES, _list, _safe_log_path, _stop, _watch


class Handle:
    def __init__(self):
        self.edits = []

    def edit(self, text):
        self.edits.append(text)
        return {"ok": True}


class Context:
    plugin_id = "babysit"

    def __init__(self):
        self.posts = []
        self.injections = []
        self.handle = Handle()

    def post_message(self, text):
        self.posts.append(text)
        return self.handle

    def inject_message(self, content, *, session_key):
        self.injections.append((content, session_key))
        return True


class PluginTests(unittest.TestCase):
    def setUp(self):
        _WATCHES.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "train.log"
        self.path.write_text("")
        self.ctx = Context()
        gateway = types.ModuleType("gateway")
        session_context = types.ModuleType("gateway.session_context")
        session_context.get_session_env = lambda name: "test-session" if name == "HERMES_SESSION_KEY" else ""
        self.modules = patch.dict(sys.modules, {
            "gateway": gateway,
            "gateway.session_context": session_context,
        })
        self.modules.start()
        self.poll = patch("babysit._POLL_SECONDS", 0.01)
        self.poll.start()

    def tearDown(self):
        for watch_id in list(_WATCHES):
            _stop({"watch_id": watch_id})
        for state in list(_WATCHES.values()):
            state["thread"].join(1)
        _WATCHES.clear()
        self.poll.stop()
        self.modules.stop()
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def start_watch(self, **args):
        result = json.loads(_watch({"log_path": str(self.path), **args}, ctx=self.ctx))
        self.assertTrue(result["ok"], result)
        return result["watch_id"]

    def test_posts_once_throttles_edits_and_injects_nan_wake(self):
        watch_id = self.start_watch()
        self.assertEqual(len(self.ctx.posts), 1)
        with self.path.open("a") as log:
            log.write("step 1\n")
            log.flush()
        with self.path.open("a") as log:
            log.write("step 2\nstep 3\nloss: nan\n")
            log.flush()
        self.assertTrue(self.wait_for(lambda: any("loss: nan" in item[0] for item in self.ctx.injections)))
        self.assertEqual(len(self.ctx.posts), 1)
        self.assertEqual(self.ctx.injections[0], ("[babysit] nan in train.log: loss: nan", "test-session"))
        self.assertEqual(len(self.ctx.handle.edits), 1)
        self.assertIn("nan: loss: nan", self.ctx.handle.edits[0])
        self.assertEqual(json.loads(_stop({"watch_id": watch_id}))["ok"], True)
        self.assertTrue(self.wait_for(lambda: watch_id not in _WATCHES))

    def test_stall_alert_and_list(self):
        watch_id = self.start_watch(stall_minutes=0.0001)
        with self.path.open("a") as log:
            log.write("starting job\n")
            log.flush()
        self.assertTrue(self.wait_for(lambda: any("stall in train.log" in item[0]
                                                  for item in self.ctx.injections)))
        self.assertIn(watch_id, json.loads(_list())["watches"][0]["watch_id"])
        self.assertEqual(len([item for item in self.ctx.injections if "stall in" in item[0]]), 1)

    def test_dead_process_event_and_explicit_exit_suppression(self):
        self.start_watch(pid=2**31 - 1)
        self.assertTrue(self.wait_for(lambda: any("process ended in train.log" in item[0]
                                                   for item in self.ctx.injections)))
        self.assertTrue(self.wait_for(lambda: not _WATCHES))
        self.ctx = Context()
        self.path.write_text("")
        self.start_watch(pid=2**31 - 1)
        with self.path.open("a") as log:
            log.write("process exited with code 0\n")
            log.flush()
        self.assertTrue(self.wait_for(lambda: not _WATCHES))
        self.assertFalse(any("process ended" in item[0] for item in self.ctx.injections))

    def test_refuses_protected_path(self):
        result = json.loads(_watch({"log_path": "/etc/passwd"}, ctx=self.ctx))
        self.assertFalse(result["ok"])
        self.assertEqual(self.ctx.posts, [])

    def test_refuses_active_profile_home(self):
        profile_home = Path(self.temp.name) / "active-profile"
        profile_home.mkdir()
        protected_log = profile_home / "private.log"
        protected_log.write_text("secret")
        with patch("hermes_constants.get_hermes_home", return_value=profile_home):
            with self.assertRaisesRegex(ValueError, "protected directory"):
                _safe_log_path(str(protected_log))


if __name__ == "__main__":
    unittest.main()
