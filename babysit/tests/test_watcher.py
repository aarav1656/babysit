import os
import tempfile
import unittest
from pathlib import Path

from babysit.watcher import EditThrottle, Event, JobWatch, tail_fd, tail_lines


class JobWatchTests(unittest.TestCase):
    def test_nan_detection_boundaries(self):
        watch = JobWatch()
        self.assertEqual([event.kind for event in watch.feed("loss: nan", 0)], ["nan"])
        self.assertEqual(watch.feed("loss=nano", 1), [])
        self.assertEqual(watch.feed("loss=infer", 2), [])

        other = JobWatch()
        self.assertEqual([event.kind for event in other.feed("loss=inf", 0)], ["nan"])
        self.assertEqual([event.kind for event in JobWatch().feed("val_loss nan", 0)], ["nan"])

    def test_nan_oom_and_traceback_each_deduplicate(self):
        watch = JobWatch()
        self.assertEqual([event.kind for event in watch.feed("loss nan", 0)], ["nan"])
        self.assertEqual(watch.feed("val_loss: inf", 1), [])

        oom_watch = JobWatch()
        self.assertEqual([event.kind for event in oom_watch.feed("CUDA out of memory", 0)], ["oom"])
        self.assertEqual(oom_watch.feed("out of memory", 1), [])

        traceback_watch = JobWatch()
        self.assertEqual(
            [event.kind for event in traceback_watch.feed("Traceback (most recent call last):", 0)],
            ["traceback"],
        )
        self.assertEqual(traceback_watch.feed("Traceback (most recent call last): again", 1), [])

    def test_oom_alert_and_traceback_matching(self):
        watch = JobWatch(extra_alert_patterns=[r"retry\s+\d+", "urgent"])
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

        explicit = JobWatch(progress_regex=r"DONE: \d+")
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

    def test_exit_events_are_immutable(self):
        watch = JobWatch()
        self.assertEqual(watch.exited(0), Event("exit_ok", "exit code 0", ""))
        self.assertEqual(watch.exited(9), Event("exit_fail", "exit code 9", ""))
        with self.assertRaises(AttributeError):
            watch.exited(0).kind = "changed"


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
