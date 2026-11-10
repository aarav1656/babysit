import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]

# Runs in a child interpreter so Hermes' loader, registry and session context are the real ones and
# none of the stubs the other test modules install are present.
SCRIPT = textwrap.dedent("""
    import json, sys, time, threading
    from pathlib import Path
    from hermes_cli.plugins import discover_plugins, invoke_hook
    from tools.registry import registry
    from gateway.session_context import set_session_vars, clear_session_vars

    discover_plugins(force=True)
    log = Path(sys.argv[1])
    out = {"registered": sorted(n for n in ("babysit_watch", "babysit_list", "babysit_stop")
                                if registry.get_entry(n) is not None)}
    out["definitions"] = {d["function"]["name"]: d["function"] for d in registry.get_definitions(
        {"babysit_watch", "babysit_list", "babysit_stop"})}
    out["unbound"] = json.loads(registry.dispatch("babysit_watch", {"log_path": str(log)}))

    def call(name, args):
        tokens = set_session_vars(platform="telegram", session_key="agent:main:telegram:dm:1",
                                  session_id="sid-loader")
        try:
            return json.loads(registry.dispatch(name, args))
        finally:
            clear_session_vars(tokens)

    out["watch"] = call("babysit_watch", {"log_path": str(log), "alert_patterns": ["boom"]})
    with log.open("a") as handle:
        handle.write("step 1 loss: nan\\n")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        watches = call("babysit_list", {})["watches"]
        if watches and watches[0]["notify_error"]:
            break
        time.sleep(0.2)
    out["listed"] = watches
    invoke_hook("on_session_finalize", session_id="sid-loader", platform="telegram")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(t.name.startswith("babysit-") for t in threading.enumerate()):
        time.sleep(0.1)
    out["threads_after_finalize"] = [t.name for t in threading.enumerate() if t.name.startswith("babysit-")]
    print("RESULT " + json.dumps(out))
""")


@unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "needs the Python environment Hermes runs in")
class HermesLoaderTests(unittest.TestCase):
    def test_real_loader_registry_dispatch_and_session_finalize(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as jobs:
            shutil.copytree(PLUGIN_DIR, Path(home) / "plugins" / "babysit",
                            ignore=shutil.ignore_patterns("__pycache__", "tests"))
            (Path(home) / "config.yaml").write_text("plugins:\n  enabled:\n    - babysit\n")
            log = Path(jobs).resolve() / "train.log"
            log.write_text("")
            env = {**os.environ, "HERMES_HOME": home}
            run = subprocess.run([sys.executable, "-c", SCRIPT, str(log)], cwd=jobs, env=env,
                                 capture_output=True, text=True, timeout=180)
            lines = [line for line in run.stdout.splitlines() if line.startswith("RESULT ")]
            self.assertTrue(lines, run.stdout[-2000:] + run.stderr[-2000:])
            out = json.loads(lines[-1][len("RESULT "):])

        self.assertEqual(out["registered"], ["babysit_list", "babysit_stop", "babysit_watch"])
        # What the model is sent: parameters the model can read, not a bare JSON schema.
        watch_def = out["definitions"]["babysit_watch"]
        self.assertTrue(watch_def["description"])
        self.assertEqual(watch_def["parameters"]["required"], ["log_path"])
        self.assertEqual(sorted(watch_def["parameters"]["properties"]),
                         ["alert_patterns", "log_path", "pid", "progress_pattern", "stall_minutes"])
        self.assertEqual(out["definitions"]["babysit_stop"]["parameters"]["required"], ["watch_id"])
        self.assertEqual(out["definitions"]["babysit_list"]["parameters"]["type"], "object")
        self.assertFalse(out["unbound"]["ok"])
        self.assertIn("needs a session", out["unbound"]["error"])
        self.assertTrue(out["watch"]["ok"], out["watch"])
        self.assertEqual(out["watch"]["status_message_error"], "gateway_unavailable")
        [watch] = out["listed"]
        self.assertEqual((watch["watch_id"], watch["lines"], watch["status"]),
                         (out["watch"]["watch_id"], 1, "step 1 loss: nan"))
        # No live gateway here: Hermes refuses the injection and the chat post, and the watch says so.
        self.assertEqual(watch["notify_error"],
                         "session injection refused and the chat post failed: gateway_unavailable")
        self.assertEqual(out["threads_after_finalize"], [])


if __name__ == "__main__":
    unittest.main()
