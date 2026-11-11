import importlib.util
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import babysit
from babysit.metrics import MetricStore

ROOT = Path(__file__).resolve().parent.parent / "babysit"
spec = importlib.util.spec_from_file_location("babysit_plugin_api", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def state_for(home, **over):
    """A watch state as ``babysit._watch`` builds it, without a thread."""
    from collections import deque

    job = babysit.watcher.JobWatch(None, 600, [])
    store = MetricStore(t0=0.0)
    for i in range(1, 41):
        store.feed(f"step {i * 100}/10000 loss={2 - i * 0.03:.3f} lr=3e-4 it/s=2", i * 0.5)
    store.mark("nan", "step 4000 loss=nan", 21.0)
    state = {
        "watch_id": "0123456789ab", "name": "train.log", "path": "/tmp/train.log", "pid": 4242, "job": job,
        "metrics": store, "started": 0.0, "lines": 40, "wakes": 1, "repeat_wakes": 1, "suppressed_total": 0,
        "tail": deque(["step 3900 loss=1.1", "step 4000 loss=nan"], maxlen=12), "notify_error": None,
        "snapshot_dir": home, "wake_delivery": "agent", "end_detail": "", "ended": None, "snapshot_at": 0.0, "dirty": True,
    }
    state.update(over)
    return state


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.folder = self.home / "plugin-data" / "babysit" / "watches"
        self.folder.mkdir(parents=True)
        patcher = patch.object(api, "get_hermes_home", lambda: self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/babysit")
        self.client = TestClient(app)

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, watch_id="0123456789ab", ended=None, age=0.0, **over):
        state = state_for(self.folder, watch_id=watch_id, **over)
        babysit._write_snapshot(state, 20.0, ended)
        path = self.folder / f"{watch_id}.json"
        if age:
            data = json.loads(path.read_text())
            data["updated"] = time.time() - age
            path.write_text(json.dumps(data))
        return path

    def get(self):
        response = self.client.get("/api/plugins/babysit/watches")
        self.assertEqual(response.status_code, 200)
        return response.json()["watches"]

    def test_publishes_a_running_watch_the_page_can_draw(self):
        self.publish()
        (watch,) = self.get()
        self.assertEqual((watch["name"], watch["state"], watch["pid"]), ("train.log", "running", 4242))
        self.assertEqual((watch["step"], watch["total"]), (4000, 10000))
        self.assertAlmostEqual(watch["frac"], 0.4)
        self.assertEqual(watch["primary"], "loss")
        self.assertEqual(list(watch["series"])[0], "loss")
        self.assertEqual(len(watch["series"]["loss"]), 40)
        self.assertEqual(watch["events"][0]["kind"], "nan")
        self.assertEqual(watch["events"][0]["x"], 4000)
        self.assertGreaterEqual(watch["events"][0]["last"], watch["events"][0]["t"])
        self.assertEqual(watch["tail"][-1], "step 4000 loss=nan")
        self.assertEqual((watch["repeat_wakes"], watch["wake_budget"], watch["model_calls"]), (1, 20, 0))
        self.assertEqual(watch["stall_seconds"], 600)
        self.assertEqual(watch["wake_delivery"], "agent")

    def test_publishes_pause_and_lineage_fields(self):
        self.publish(paused_at=time.time() - 12, paused_reason="nan", paused_step=60, on_failure="pause",
                     lineage=[{"attempt": 1, "watch_id": "aaaaaaaaaaaa", "name": "train.log",
                               "failure_kind": "nan", "failure_step": 60, "verdict": None},
                              {"attempt": 2, "watch_id": "bbbbbbbbbbbb", "name": "train.log",
                               "failure_kind": None, "failure_step": None, "verdict": "recovered"}],
                     verdict={"kind": "recovered", "attempt": 2, "step": 61, "failure_step": 60})
        (watch,) = self.get()
        self.assertEqual((watch["paused"], watch["paused_reason"], watch["paused_step"], watch["on_failure"]),
                         (True, "nan", 60, "pause"))
        self.assertEqual(watch["lineage"][1]["verdict"], "recovered")
        self.assertEqual(watch["verdict"]["kind"], "recovered")

    def test_the_snapshot_file_is_owner_only_and_the_folder_is_not_world_readable(self):
        path = self.publish()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(os.listdir(self.folder), [path.name])  # no temp file left behind

    def test_route_is_read_only(self):
        self.publish()
        methods = {method for route in api.router.routes for method in route.methods}
        self.assertEqual(methods, {"GET"})
        before = sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in self.folder.iterdir())
        for _ in range(3):
            self.get()
        after = sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in self.folder.iterdir())
        self.assertEqual(before, after)
        for verb in ("post", "put", "delete", "patch"):
            self.assertEqual(getattr(self.client, verb)("/api/plugins/babysit/watches").status_code, 405)

    def test_route_creates_nothing_when_no_watch_ever_ran(self):
        for item in self.folder.iterdir():
            item.unlink()
        self.folder.rmdir()
        self.folder.parent.rmdir()
        self.assertEqual(self.get(), [])
        self.assertFalse(self.folder.exists())

    def test_silence_marks_a_running_watch_lost_and_ended_ones_age_out(self):
        self.publish("aaaaaaaaaaaa", age=60)
        self.publish("bbbbbbbbbbbb", ended="process ended", age=3600)
        self.publish("cccccccccccc", ended="process ended", age=7 * 3600)
        states = {w["watch_id"]: w["state"] for w in self.get()}
        self.assertEqual(states, {"aaaaaaaaaaaa": "lost", "bbbbbbbbbbbb": "process ended"})

    def test_running_sorts_first_then_newest(self):
        self.publish("111111111111", ended="stopped", age=10)
        self.publish("222222222222", age=1)
        self.publish("333333333333", ended="stopped", age=2)
        self.assertEqual([w["watch_id"] for w in self.get()], ["222222222222", "333333333333", "111111111111"])

    def test_garbage_files_are_skipped_not_fatal(self):
        self.publish()
        (self.folder / "ffffffffffff.json").write_text("{not json")
        (self.folder / "eeeeeeeeeeee.json").write_text(json.dumps({"v": 2}))
        (self.folder / "dddddddddddd.json").write_text(json.dumps([1, 2]))
        (self.folder / "cccccccccccc.json").write_bytes(b"\xff\xfe\x00")
        (self.folder / "not-a-watch.json").write_text(json.dumps({"v": 1, "state": "running"}))
        (self.folder / "b16b16b16b16.json").write_bytes(b" " * (api._MAX_BYTES + 10))
        self.assertEqual([w["watch_id"] for w in self.get()], ["0123456789ab"])

    def test_a_symlinked_snapshot_is_not_followed(self):
        secret = self.home / "secret.json"
        secret.write_text(json.dumps({"v": 1, "state": "running", "updated": time.time(), "name": "STOLEN"}))
        os.symlink(secret, self.folder / "999999999999.json")
        self.assertEqual(self.get(), [])

    def test_hostile_field_values_are_typed_or_dropped(self):
        path = self.publish()
        data = json.loads(path.read_text())
        data.update(name="x" * 5000, step="4000", frac=True, eta=float("inf"), pid="1; rm -rf /",
                    tail=["<img src=x onerror=alert(1)>"] * 50, wakes=None)
        data["series"] = {"loss": [[1, 2, 3], ["a", 2, 3], [1, 2], [1, 2, True], [1, 2, None]] + [[1, 2, 3]] * 900}
        data["events"] = [{"x": "1", "kind": "k" * 500, "text": "t" * 500}] + ["junk"] * 3
        path.write_text(json.dumps(data))
        (watch,) = self.get()
        self.assertEqual(len(watch["name"]), 120)
        self.assertIsNone(watch["step"])
        self.assertIsNone(watch["frac"])
        self.assertIsNone(watch["eta"])
        self.assertIsNone(watch["pid"])
        self.assertEqual(len(watch["tail"]), 12)
        self.assertEqual(watch["tail"][0], "<img src=x onerror=alert(1)>")  # text stays text: the page renders it as a text node
        self.assertEqual(len(watch["series"]["loss"]), 600 - 4)
        self.assertEqual(len(watch["events"][0]["kind"]), 40)
        self.assertEqual(len(watch["events"]), 1)
        self.assertEqual(watch["wakes"], 0)

    def test_response_is_strict_json(self):
        self.publish()
        text = self.client.get("/api/plugins/babysit/watches").text
        json.loads(text, parse_constant=lambda name: self.fail(name))

    def test_at_most_sixteen_watches(self):
        for i in range(20):
            self.publish(f"{i:012x}")
        self.assertEqual(len(self.get()), 16)

    def test_writer_and_reader_agree_on_the_folder_name(self):
        from plugins.plugin_storage import plugin_data_dir

        with patch("hermes_constants.get_hermes_home", lambda: self.home):
            self.assertEqual(plugin_data_dir("babysit") / "watches", api._folder())


class WriterTests(unittest.TestCase):
    def test_write_failure_stops_publishing_and_says_so_without_raising(self):
        with tempfile.TemporaryDirectory() as temp:
            gone = Path(temp) / "gone"
            state = state_for(gone, notify_error=None)
            babysit._write_snapshot(state, 20.0)
            self.assertIsNone(state["snapshot_dir"])
            self.assertEqual(state["notify_error"], "dashboard snapshot failed: FileNotFoundError")

    def test_old_snapshots_are_pruned_and_only_snapshots_are_touched(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for i in range(30):
                (folder / f"{i:012x}.json").write_text("{}")
                os.utime(folder / f"{i:012x}.json", (time.time() - i, time.time() - i))
            (folder / "a00000000000.json").write_text("{}")
            os.utime(folder / "a00000000000.json", (1, 1))
            (folder / "keepme.txt").write_text("x")
            babysit._prune_snapshots(folder)
            names = sorted(p.name for p in folder.iterdir())
            self.assertIn("keepme.txt", names)
            self.assertNotIn("a00000000000.json", names)
            self.assertEqual(len([n for n in names if n.endswith(".json")]), 24)


if __name__ == "__main__":
    unittest.main()
