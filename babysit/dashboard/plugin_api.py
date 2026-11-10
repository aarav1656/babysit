"""Babysit's read-only dashboard route, mounted at /api/plugins/babysit/.

Watches live in the memory of the process that runs the session, and that is often not this one: the
TUI, the messaging gateway and the classic CLI each host their own, while this route runs in the
dashboard or Desktop gateway process. So each watch publishes its state as a small JSON file under
``<hermes home>/plugin-data/babysit/watches`` (owner-only, replaced atomically) and this route only
reads those files. It writes nothing, creates nothing and takes no input.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from pathlib import Path

from fastapi import APIRouter

from hermes_constants import get_hermes_home

router = APIRouter()

_NAME = re.compile(r"^[0-9a-f]{12}\.json$")
_MAX_BYTES = 1 << 20
_LOST_AFTER = 20.0  # a running watch republishes at least every 4 s; silence means its process is gone
_ENDED_KEEP = 6 * 60 * 60
_LIMIT = 16
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
_STATES = frozenset({"running", "stopped", "process ended", "watch expired"})


def _folder() -> Path:
    """The folder ``plugins.plugin_storage.plugin_data_dir("babysit")`` names, without creating it."""
    return get_hermes_home() / "plugin-data" / "babysit" / "watches"


def _num(value, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return value


def _text(value, limit: int = 300) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _points(raw) -> list:
    out = []
    for item in raw[:600] if isinstance(raw, list) else ():
        if isinstance(item, list) and len(item) == 3 and all(_num(part) is not None for part in item):
            out.append(item)
    return out


def _mem(raw) -> dict | None:
    if isinstance(raw, dict) and _num(raw.get("rss")) is not None and _num(raw.get("limit")):
        return {"rss": raw["rss"], "limit": raw["limit"]}
    return None


def _watch(raw: object, now: float) -> dict | None:
    """The fields of one snapshot the page uses, each checked, or None for a file that is not one."""
    if not isinstance(raw, dict) or raw.get("v") != 1:
        return None
    state = raw.get("state")
    updated = _num(raw.get("updated"), 0.0)
    if state not in _STATES:
        return None
    if state == "running" and now - updated > _LOST_AFTER:
        state = "lost"
    series = raw.get("series") if isinstance(raw.get("series"), dict) else {}
    events = [
        {"x": _num(e.get("x"), 0), "t": _num(e.get("t"), 0), "kind": _text(e.get("kind"), 40),
         "text": _text(e.get("text"), 160), "count": _num(e.get("count"), 1)}
        for e in (raw.get("events") if isinstance(raw.get("events"), list) else [])[:64] if isinstance(e, dict)
    ]
    epoch = raw.get("epoch")
    return {
        "watch_id": _text(raw.get("watch_id"), 12), "name": _text(raw.get("name"), 120),
        "path": _text(raw.get("path"), 500), "pid": _num(raw.get("pid")),
        "state": state, "updated": updated, "age": round(max(0.0, now - updated), 1),
        "uptime": _num(raw.get("uptime"), 0), "lines": _num(raw.get("lines"), 0),
        "wakes": _num(raw.get("wakes"), 0), "repeat_wakes": _num(raw.get("repeat_wakes"), 0),
        "wake_budget": _num(raw.get("wake_budget"), 0), "suppressed": _num(raw.get("suppressed"), 0),
        "step": _num(raw.get("step")), "total": _num(raw.get("total")), "frac": _num(raw.get("frac")),
        "eta": _num(raw.get("eta")), "rate": _num(raw.get("rate")),
        "epoch": [_num(part) for part in epoch[:2]] if isinstance(epoch, list) else None,
        "primary": _text(raw.get("primary"), 60) or None,
        "series": {_text(key, 60): _points(points) for key, points in list(series.items())[:8]},
        "events": events, "tail": [_text(line, 300) for line in (raw.get("tail") or [])[:12]],
        "notify_error": _text(raw.get("notify_error")) or None,
        "stall_seconds": _num(raw.get("stall_seconds")), "model_calls": _num(raw.get("model_calls"), 0),
        "wake_delivery": "chat" if raw.get("wake_delivery") == "chat" else "agent",
        "wake_note": _text(raw.get("wake_note"), 400) or None, "detail": _text(raw.get("detail"), 300) or None,
        "mem": _mem(raw.get("mem")),
    }


def _read(path: Path) -> object:
    fd = os.open(path, _OPEN_FLAGS)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            return None
        return json.loads(os.read(fd, _MAX_BYTES))
    finally:
        os.close(fd)


@router.get("/watches")
def watches() -> dict:
    """Every watch this profile published: running ones first, then those that ended in the last 6 hours."""
    now = time.time()
    found = []
    try:
        names = [name for name in os.listdir(_folder()) if _NAME.match(name)]
    except OSError:
        names = []
    for name in names:
        try:
            watch = _watch(_read(_folder() / name), now)
        except (OSError, ValueError):
            continue
        if watch and (watch["state"] == "running" or watch["age"] < _ENDED_KEEP):
            found.append(watch)
    found.sort(key=lambda item: (item["state"] != "running", -item["updated"]))
    return {"now": now, "watches": found[:_LIMIT]}
