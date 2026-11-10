import os
import tempfile
import time
import unittest
from pathlib import Path

from babysit.watcher import EditThrottle, JobWatch, tail_fd


def tail_lines(path, offset):
    fd = os.open(path, os.O_RDONLY)
    try:
        return tail_fd(fd, offset)
    finally:
        os.close(fd)


class JobWatchTests(unittest.TestCase):
    def test_nan_detection_boundaries(self):
        watch = JobWatch()
        self.assertEqual([event.kind for event in watch.feed("loss: nan", 0)], ["nan"])
        self.assertEqual(watch.feed("loss=nano", 1), [])
        self.assertEqual(watch.feed("loss=infer", 2), [])

        other = JobWatch()
        self.assertEqual([event.kind for event in other.feed("loss=inf", 0)], ["nan"])
        self.assertEqual([event.kind for event in JobWatch().feed("val_loss nan", 0)], ["nan"])

    def test_non_finite_fires_on_metric_values(self):
        for line in ("step 81 epoch 1 loss=nan lr=0.1000", "val_loss: inf", "grad_norm: nan", "valLoss=nan",
                     "train/loss: -inf", "gradNorm = Infinity", "epoch 3 | 0.52 nan 0.33", "eval acc=-nan"):
            with self.subTest(line=line):
                self.assertEqual([event.kind for event in JobWatch().feed(line, 0)], ["nan"])

    def test_non_finite_ignores_config_style_lines(self):
        for line in ("max_grad_norm: inf", "--max_grad_norm=inf", "clip: inf", "grad_clip = inf",
                     "loss_threshold: inf", "timeout=inf", "fill_value: nan", "nan_policy: omit",
                     "Namespace(lr=0.1, max_steps=inf)", "min_lr: -inf", "lr: 0.1  clip: inf"):
            with self.subTest(line=line):
                self.assertEqual(JobWatch().feed(line, 0), [])

    def test_nan_oom_and_traceback_fire_every_time(self):
        for first, second in (("loss nan", "val_loss: inf"), ("CUDA out of memory", "out of memory"),
                              ("Traceback (most recent call last):", "Traceback (most recent call last):")):
            with self.subTest(first=first):
                watch = JobWatch()
                kinds = [event.kind for event in watch.feed(first, 0)]
                self.assertEqual(len(kinds), 1)
                self.assertEqual([event.kind for event in watch.feed(second, 1)], kinds)

    def test_oom_alert_and_traceback_matching(self):
        watch = JobWatch(alert_patterns=["retry 3", "urgent"])
        for line in ("OutOfMemoryError", "MemoryError", "Killed", "please: out of memory"):
            fresh = JobWatch()
            self.assertEqual([event.kind for event in fresh.feed(line, 1)], ["oom"])
        self.assertEqual(watch.feed("prefix Traceback (most recent call last)", 2), [])
        alert = watch.feed("retry 3, urgent", 3)
        self.assertEqual([event.kind for event in alert], ["alert"])
        self.assertEqual(alert[0].line, "retry 3, urgent")

    def test_progress_status_default_explicit_and_trimmed(self):
        watch = JobWatch()
        self.assertIsNone(watch.status)
        watch.feed("starting", 0)
        watch.feed("Step 8 of 20", 1)
        self.assertEqual(watch.status, "Step 8 of 20")
        watch.feed("download 42%", 2)
        self.assertEqual(watch.status, "download 42%")
        watch.feed("epoch 3 complete " + "x" * 220, 3)
        self.assertEqual(len(watch.status), 200)

        explicit = JobWatch(progress_pattern="DONE: ")
        explicit.feed("step 4", 0)
        self.assertIsNone(explicit.status)
        explicit.feed("DONE: 7" + "x" * 220, 1)
        self.assertEqual(len(explicit.status), 200)
        self.assertTrue(explicit.status.startswith("DONE: 7"))

    def test_stall_fires_once_and_rearms_after_feed(self):
        watch = JobWatch(stall_seconds=10)
        self.assertEqual(watch.tick(50), [])
        watch.feed("working", 100)
        self.assertEqual(watch.tick(109.9), [])
        events = watch.tick(110)
        self.assertEqual([event.kind for event in events], ["stall"])
        self.assertEqual(watch.tick(200), [])
        watch.feed("still working", 201)
        self.assertEqual(watch.tick(210), [])
        self.assertEqual([event.kind for event in watch.tick(211)], ["stall"])

    def test_caller_patterns_are_literal_text_not_regex(self):
        watch = JobWatch(progress_pattern="(a+)+$", alert_patterns=["(a+)+$", "x.y"])
        started = time.perf_counter()
        self.assertEqual(watch.feed("a" * 64 + "b", 0), [])
        self.assertLess(time.perf_counter() - started, 0.1)
        self.assertIsNone(watch.status)
        self.assertEqual(watch.feed("xzy", 1), [])
        self.assertEqual([event.kind for event in watch.feed("got x.y here", 2)], ["alert"])
        watch.feed("phase (a+)+$ reached", 3)
        self.assertEqual(watch.status, "phase (a+)+$ reached")

    def test_adversarial_long_lines_scan_in_linear_time(self):
        size = 256 * 1024  # quadratic patterns take minutes here, linear ones well under a second
        lines = {
            "digits": "1" * size, "dotted digits": "1." * (size // 2), "path-like name": "a/" * (size // 2),
            "spaced digits": "7 " * (size // 2), "step then spaces": "step" + " " * size + "x",
            "name then spaces": "loss" + " " * size + "x", "letters": "a" * size,
            "repeated nan": "nan " * (size // 4), "repeated keyed nan": "loss nan " * (size // 9),
            "number then nan": "1 " * (size // 2) + "nan", "camel case": "aA" * (size // 2) + " nan",
        }
        for name, line in lines.items():
            with self.subTest(shape=name):
                started = time.perf_counter()
                JobWatch(alert_patterns=["boom"]).feed(line, 0)
                self.assertLess(time.perf_counter() - started, 2.0)


class EditThrottleTests(unittest.TestCase):
    def test_first_changed_text_interval_and_identical_text(self):
        throttle = EditThrottle(min_interval=5)
        self.assertTrue(throttle.should_edit("first", 0))
        self.assertFalse(throttle.should_edit("first", 10))
        self.assertFalse(throttle.should_edit("second", 4.99))
        self.assertTrue(throttle.should_edit("second", 5))
        self.assertFalse(throttle.should_edit("third", 9.99))
        self.assertTrue(throttle.should_edit("third", 10))


class TailLinesTests(unittest.TestCase):
    def test_complete_lines_partial_line_and_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.log"
            path.write_bytes(b"one\ntwo\npart")
            lines, offset = tail_lines(path, 0)
            self.assertEqual(lines, ["one", "two"])
            self.assertEqual(offset, len(b"one\ntwo\n"))

            with path.open("ab") as log:
                log.write(b"ial\nthree\nnext")
            lines, next_offset = tail_lines(path, offset)
            self.assertEqual(lines, ["partial", "three"])
            self.assertEqual(next_offset, len(b"one\ntwo\npartial\nthree\n"))

            path.write_bytes(b"new\n")
            lines, final_offset = tail_lines(path, next_offset)
            self.assertEqual(lines, ["new"])
            self.assertEqual(final_offset, len(b"new\n"))

    def test_empty_and_crlf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.log"
            path.write_bytes(b"\r\nline\r\n")
            self.assertEqual(tail_lines(path, 0), (["", "line"], len(b"\r\nline\r\n")))

    def test_invalid_utf8_is_replaced_not_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.log"
            path.write_bytes(b"ok\n\xff\xfe bad\nloss: nan\n")
            lines, offset = tail_lines(path, 0)
            self.assertEqual(lines, ["ok", "\ufffd\ufffd bad", "loss: nan"])
            self.assertEqual(offset, path.stat().st_size)

    def test_read_is_capped_per_call_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.log"
            path.write_bytes(b"aaaa\nbbbb\ncccc\ndddd\n")
            fd = os.open(path, os.O_RDONLY)
            try:
                first, offset = tail_fd(fd, 0, max_bytes=12)
                self.assertEqual((first, offset), (["aaaa", "bbbb"], 10))
                seen = first
                for _ in range(10):
                    lines, offset = tail_fd(fd, offset, max_bytes=12)
                    seen += lines
                self.assertEqual(seen, ["aaaa", "bbbb", "cccc", "dddd"])
                self.assertEqual(offset, path.stat().st_size)
            finally:
                os.close(fd)

    def test_line_longer_than_the_cap_does_not_stall(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.log"
            path.write_bytes(b"x" * 20 + b"\nnext\n")
            fd = os.open(path, os.O_RDONLY)
            try:
                lines, offset = tail_fd(fd, 0, max_bytes=8)
                self.assertEqual((lines, offset), (["x" * 8], 8))
                seen = []
                for _ in range(10):
                    lines, offset = tail_fd(fd, offset, max_bytes=8)
                    seen += lines
                self.assertEqual(seen[-1], "next")
                self.assertEqual(offset, path.stat().st_size)
            finally:
                os.close(fd)

    def test_reads_the_open_file_not_the_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.log"
            other = Path(directory) / "other.log"
            path.write_bytes(b"original\n")
            other.write_bytes(b"replacement\n")
            fd = os.open(path, os.O_RDONLY)
            try:
                os.replace(other, path)
                self.assertEqual(tail_fd(fd, 0), (["original"], 9))
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
