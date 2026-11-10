"""Numeric metrics from job log lines: a bounded per-watch store, progress rate and ETA.

Every pattern here is linear on a 1 MiB line. A name or a number only starts where the previous
character cannot extend it, digit runs and gaps are length-capped, and no two adjacent quantifiers can
match the same characters, so no run of digits, spaces or name characters is rescanned.
"""

import math
import re
from collections import deque
from itertools import islice

from .watcher import _KEY_PARTS, _SETTING_WORDS

MAX_POINTS = 600  # samples kept per metric
MAX_KEYS = 32  # distinct metric names kept per watch
MAX_PAIRS = 48  # key=value pairs read from one line
MAX_EVENTS = 64
_EVENT_FOLD = 10.0  # seconds
_MIN_SPAN = 2.0  # seconds of progress needed before a measured rate is believed

_NUMBER = r"[+-]?(?:\d{1,18}(?:\.\d{1,18})?|\.\d{1,18})(?:[eE][+-]?\d{1,3})?"
_KV = re.compile(rf"(?<![\w./-])([A-Za-z_][\w./-]{{0,39}})[ \t]{{0,3}}[:=][ \t]{{0,3}}({_NUMBER})(?!\w)")
_COUNT = r"(\d{1,18})(?:[ \t]{0,3}(?:/|of)[ \t]{0,3}(\d{1,18}))?(?!\w)"
_STEP = re.compile(rf"(?<![\w./-])(?:global[_ ]?step|step|iter(?:ation)?)s?[ \t]{{0,3}}[:=#]?[ \t]{{0,3}}{_COUNT}", re.I)
_EPOCH = re.compile(rf"(?<![\w./-])epochs?[ \t]{{0,3}}[:=#]?[ \t]{{0,3}}{_COUNT}", re.I)
# tqdm: " 41%|████      | 4120/10000 [12:30<17:40,  1.92it/s]"
_TQDM = re.compile(r"(?<![\w.])(\d{1,18})/(\d{1,18})[ \t]{0,2}\[")
_PERCENT = re.compile(
    r"(?<![\w.])(\d{1,3}(?:\.\d{1,3})?)[ \t]{0,2}%(?=[ \t]{0,2}\|)"
    r"|(?:progress|done|complete[d]?)[ \t]{0,3}[:=]?[ \t]{0,3}(\d{1,3}(?:\.\d{1,3})?)[ \t]{0,2}%", re.I)
_RATE = re.compile(r"(?<![\w.])(\d{1,9}(?:\.\d{1,9})?)[ \t]{0,2}(it/s|s/it|steps?/s)(?!\w)", re.I)
_STEP_KEYS = frozenset({"step", "steps", "iter", "iters", "iteration", "global_step", "epoch", "epochs"})
_RATE_KEYS = frozenset({"it/s", "its", "it_s", "iter/s", "iter_per_sec", "steps_per_sec", "step/s", "steps/s"})


def _is_setting(key: str) -> bool:
    return bool({word.lower() for word in _KEY_PARTS.split(key) if word} & _SETTING_WORDS)


def primary_key(keys) -> str | None:
    """The metric a chart or card leads with: the training loss, else any loss, else the first key."""
    keys = list(keys)
    if "loss" in keys:
        return "loss"
    for key in keys:
        if "loss" in key and not key.startswith(("val", "eval", "test", "valid")):
            return key
    return next((key for key in keys if "loss" in key), keys[0] if keys else None)


def _thin(points: list, limit: int) -> list:
    """At most ``limit`` points, evenly spaced, always ending on the newest."""
    if len(points) <= limit:
        return points
    stride = (len(points) - 1) / (limit - 1)
    return [points[round(i * stride)] for i in range(limit)]


class MetricStore:
    def __init__(self, max_points: int = MAX_POINTS, t0: float | None = None):
        self.max_points = max_points
        self.series: dict[str, deque] = {}  # key -> (x, seconds since the first line, value)
        self.events: deque = deque(maxlen=MAX_EVENTS)  # [x, seconds, kind, text, count, seconds of the latest repeat]
        self.step: int | None = None
        self.total: int | None = None
        self.frac: float | None = None
        self.epoch: tuple[int, int | None] | None = None
        self.hint: float | None = None  # steps per second the job itself printed
        self.x = 0  # where the newest sample sits on the x axis: the step, or the line count
        self.lines = 0
        self.t0 = t0  # the clock every sample time and event time counts from
        self._progress: deque = deque(maxlen=64)  # (time, fraction done) each time the fraction moved

    def feed(self, line: str, now: float) -> None:
        if self.t0 is None:
            self.t0 = now
        self.lines += 1
        self._read_progress(line, now)
        self.x = self.step if self.step is not None else self.lines
        for match in islice(_KV.finditer(line), MAX_PAIRS):
            key = match.group(1).lower()
            if key in _STEP_KEYS or _is_setting(key):
                continue
            value = float(match.group(2))
            if not math.isfinite(value):
                continue
            if key in _RATE_KEYS:
                self.hint = value if value > 0 else self.hint
                continue
            points = self.series.get(key)
            if points is None:
                if len(self.series) >= MAX_KEYS:
                    continue
                points = self.series[key] = deque(maxlen=self.max_points)
            points.append((self.x, now - self.t0, value))

    def _read_progress(self, line: str, now: float) -> None:
        step = total = None
        if match := _STEP.search(line):
            step, total = int(match.group(1)), int(match.group(2) or 0)
        elif match := _TQDM.search(line):
            step, total = int(match.group(1)), int(match.group(2))
        if total:
            self.total = total
        if step is not None:
            self.step = step
        frac = min(1.0, step / self.total) if step is not None and self.total else None
        if frac is None and (match := _PERCENT.search(line)):
            frac = min(1.0, float(match.group(1) or match.group(2)) / 100)
        if frac is not None:
            self.frac = frac
            if not self._progress or frac != self._progress[-1][1]:
                self._progress.append((now, frac))
        if match := _EPOCH.search(line):
            self.epoch = (int(match.group(1)), int(match.group(2) or 0) or None)
        if match := _RATE.search(line):
            value = float(match.group(1))
            if value > 0:
                self.hint = 1 / value if match.group(2).lower() == "s/it" else value

    def mark(self, kind: str, text: str, now: float) -> None:
        """Record an event on the chart at the newest x. Repeats of one kind less than ``_EVENT_FOLD``
        seconds apart fold into the first marker as a count, so a log that prints nan on every line
        cannot push the earlier events out."""
        t = now - (self.t0 if self.t0 is not None else now)
        for event in reversed(self.events):
            if event[2] == kind:
                if t - event[5] < _EVENT_FOLD:
                    event[4] += 1
                    event[5] = t
                    return
                break
        self.events.append([self.x, t, kind, text[:120], 1, t])

    def values(self, key: str, last: int | None = None) -> list[float]:
        points = self.series.get(key, ())
        return [value for _x, _t, value in (islice(points, max(0, len(points) - last), None) if last else points)]

    def fraction_rate(self) -> float | None:
        """Fraction of the job done per second: measured over the recent progress, else what the job printed."""
        if len(self._progress) >= 2:
            (t0, f0), (t1, f1) = self._progress[0], self._progress[-1]
            if t1 - t0 >= _MIN_SPAN and f1 > f0:
                return (f1 - f0) / (t1 - t0)
        if self.hint and self.total:
            return self.hint / self.total
        return None

    def eta(self) -> float | None:
        """Seconds left, or None while the total or the rate is unknown."""
        rate = self.fraction_rate()
        if self.frac is None or not rate:
            return None
        return max(0.0, (1 - self.frac) / rate)

    def rate(self) -> float | None:
        """Steps per second: the job's own number when it prints one."""
        if self.hint:
            return self.hint
        rate = self.fraction_rate()
        return rate * self.total if rate and self.total else None

    def snapshot(self, max_points: int = 240, max_series: int = 8) -> dict:
        """JSON-ready view for the dashboard route: newest first by relevance, thinned to ``max_points``."""
        lead = primary_key(key for key, points in self.series.items() if len(points) >= 2)
        order = sorted((key for key in self.series if key != lead), key=lambda key: -len(self.series[key]))
        keys = ([lead] if lead else []) + order
        series = {}
        for key in keys[:max_series]:
            series[key] = [[x, round(t, 1), float(f"{v:.6g}")] for x, t, v in _thin(list(self.series[key]), max_points)]
        eta = self.eta()
        return {
            "step": self.step, "total": self.total, "frac": self.frac, "eta": eta,
            "rate": self.rate(), "epoch": list(self.epoch) if self.epoch else None,
            "primary": lead, "series": series,
            "events": [{"x": x, "t": round(t, 1), "kind": kind, "text": text, "count": count}
                       for x, t, kind, text, count, _last in self.events],
        }
