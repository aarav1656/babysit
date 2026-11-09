import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Event:
    kind: str
    detail: str
    line: str


class JobWatch:
    def __init__(
        self,
        progress_regex: str | None = None,
        stall_seconds: float = 600,
        extra_alert_patterns: list[str] = [],
    ):
        self.progress_pattern = re.compile(progress_regex) if progress_regex is not None else None
        self.stall_seconds = stall_seconds
        self.alert_patterns = [re.compile(pattern) for pattern in extra_alert_patterns]
        self.status: str | None = None
        self.last_line_time: float | None = None
        self.stalled = False
        self.seen: set[str] = set()

    def feed(self, line: str, now: float) -> list[Event]:
        self.last_line_time = now
        self.stalled = False
        if self.progress_pattern:
            progress = self.progress_pattern.search(line)
        else:
            progress = re.search(
                r"(?:\d+(?:\.\d+)?\s*%|\b(?:step|epoch|iter)\s*[:=#]?\s*\d+|\d+\s*\b(?:step|epoch|iter)\b)",
                line,
                re.IGNORECASE,
            )
        if progress:
            self.status = line.strip()[:200]

        events = []
        if re.search(
            r"\b[A-Za-z_][\w./-]*(?:\s*[:=]\s*|\s+)(?:nan|inf(?:inity)?)\b",
            line,
            re.IGNORECASE,
        ):
            events.extend(self._once("nan", "non-finite metric value", line))
        if re.search(
            r"CUDA out of memory|OutOfMemoryError|MemoryError|^\s*Killed\s*$|out of memory",
            line,
            re.IGNORECASE,
        ):
            events.extend(self._once("oom", "out of memory", line))
        if any(pattern.search(line) for pattern in self.alert_patterns):
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

    def exited(self, code: int) -> Event:
        return Event("exit_ok" if code == 0 else "exit_fail", f"exit code {code}", "")


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


def tail_lines(path, offset: int) -> tuple[list[str], int]:
    with Path(path).open("rb") as log:
        log.seek(0, 2)
        size = log.tell()
        if size < offset:
            offset = 0
        log.seek(offset)
        data = log.read()

    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return [], offset
    complete = data[: last_newline + 1]
    lines = [line.decode("utf-8").removesuffix("\r") for line in complete.splitlines()]
    return lines, offset + len(complete)
