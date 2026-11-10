import os
import re
from dataclasses import dataclass


# Lines run up to 1 MiB, so every built-in pattern stays linear: a number or a metric name only starts
# where the previous character cannot extend it, so no run of digits or name characters is rescanned.
_PROGRESS = re.compile(
    r"(?<!\d)\d+(?:\.\d+)?\s*%|\b(?:step|epoch|iter)\s*(?:[:=#]\s*)?\d+|(?<!\d)\d+\s*\b(?:step|epoch|iter)\b",
    re.IGNORECASE,
)
_NON_FINITE = re.compile(
    r"(?<![\w./-])(?=[\w./-]*[A-Za-z_])[\w./-]+(?:\s*[:=]\s*|\s+)(?:nan|inf(?:inity)?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Event:
    kind: str
    detail: str
    line: str


class JobWatch:
    def __init__(
        self,
        progress_pattern: str | None = None,
        stall_seconds: float = 600,
        alert_patterns: tuple[str, ...] = (),
    ):
        # Caller-supplied patterns are plain substrings, like core watch_patterns: a regex chosen by
        # the model could backtrack for hours on one log line while holding the interpreter lock.
        self.progress_pattern = progress_pattern
        self.stall_seconds = stall_seconds
        self.alert_patterns = tuple(alert_patterns)
        self.status: str | None = None
        self.last_line_time: float | None = None
        self.stalled = False
        self.seen: set[str] = set()

    def feed(self, line: str, now: float) -> list[Event]:
        self.last_line_time = now
        self.stalled = False
        if self.progress_pattern is not None:
            progress = self.progress_pattern in line
        else:
            progress = _PROGRESS.search(line)
        if progress:
            self.status = line.strip()[:200]

        events = []
        if _NON_FINITE.search(line):
            events.extend(self._once("nan", "non-finite metric value", line))
        if re.search(
            r"CUDA out of memory|OutOfMemoryError|MemoryError|^\s*Killed\s*$|out of memory",
            line,
            re.IGNORECASE,
        ):
            events.extend(self._once("oom", "out of memory", line))
        if any(pattern in line for pattern in self.alert_patterns):
            events.append(Event("alert", "alert pattern matched", line))
        if line.startswith("Traceback (most recent call last)"):
            events.extend(self._once("traceback", "traceback detected", line))
        return events

    def _once(self, kind: str, detail: str, line: str) -> list[Event]:
        if kind in self.seen:
            return []
        self.seen.add(kind)
        return [Event(kind, detail, line)]

    def tick(self, now: float) -> list[Event]:
        if (
            self.last_line_time is not None
            and not self.stalled
            and now - self.last_line_time >= self.stall_seconds
        ):
            self.stalled = True
            return [Event("stall", "no log output", "")]
        return []


class EditThrottle:
    def __init__(self, min_interval: float = 5.0):
        self.min_interval = min_interval
        self.last_text: str | None = None
        self.last_edit_time: float | None = None

    def should_edit(self, text: str, now: float) -> bool:
        if self.last_text == text:
            return False
        if self.last_edit_time is not None and now - self.last_edit_time < self.min_interval:
            return False
        self.last_text = text
        self.last_edit_time = now
        return True


def tail_fd(fd: int, offset: int, max_bytes: int = 1 << 20) -> tuple[list[str], int]:
    """New complete lines from an open log fd, at most ``max_bytes`` per call.

    Reads through the descriptor, never a path, so what was validated at open is what is read.
    Only one thread may read a given fd (it seeks)."""
    size = os.fstat(fd).st_size
    if size < offset:
        offset = 0
    os.lseek(fd, offset, os.SEEK_SET)
    data = os.read(fd, min(size - offset, max_bytes))
    last_newline = data.rfind(b"\n")
    if last_newline >= 0:
        complete = data[: last_newline + 1]
    elif len(data) == max_bytes:
        complete = data  # one line longer than the cap: take it rather than stall forever
    else:
        return [], offset
    lines = [line.decode("utf-8", "replace").removesuffix("\r") for line in complete.splitlines()]
    return lines, offset + len(complete)
