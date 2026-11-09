"""Hermes plugin for monitoring long-running job logs."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

from . import watcher

_POLL_SECONDS = 2.0
_MAX_AGE_SECONDS = 72 * 60 * 60
_MAX_WATCHES = 8
_LOCK = threading.RLock()
_STARTING = 0
_WATCHES: dict[str, dict] = {}
_LOGGER = logging.getLogger(__name__)


def _safe_log_path(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("log_path must be an existing regular file")
    protected = [Path("/etc").resolve(), (Path.home() / ".ssh").resolve()]
    from hermes_constants import get_hermes_home

    hermes_home = Path(get_hermes_home()).expanduser().resolve()
    protected.append(hermes_home)
    if any(path == root or root in path.parents for root in protected):
        raise ValueError("log_path is inside a protected directory")
    return path


def _status(name: str, state: dict, elapsed: float) -> str:
    last = state["job"].status or state["last_line"] or "Waiting for log output"
    return f"{name}: {last}\nElapsed: {elapsed:.0f}s | Lines seen: {state['lines']}"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _watch_loop(watch_id: str, state: dict) -> None:
    path = state["path"]
    offset = 0
    started = time.monotonic()
    throttle = watcher.EditThrottle()
    try:
        while not state["stop"].wait(_POLL_SECONDS):
            now = time.monotonic()
            if now - started >= _MAX_AGE_SECONDS:
                break
            try:
                lines, offset = watcher.tail_lines(path, offset)
            except OSError:
                lines = []
            for line in lines:
                state["lines"] += 1
                state["last_line"] = line.strip()[:200]
                if re.search(r"\b(?:exit(?:ed)?|finished)\b.*?\b(?:code|status)?\s*[:=]?\s*-?\d+\b",
                             line, re.IGNORECASE):
                    state["exit_seen"] = True
                for event in state["job"].feed(line, now):
                    _handle_event(state, event.kind, event.line, now, throttle)
            for event in state["job"].tick(now):
                _handle_event(state, event.kind, event.line, now, throttle)
            if state["pid"] is not None and not _process_alive(state["pid"]):
                if not state["exit_seen"]:
                    _handle_event(state, "process ended", state["last_line"], now, throttle)
                break
            text = _status(state["name"], state, now - started)
            if now - started >= throttle.min_interval and throttle.should_edit(text, now):
                _quiet_edit(state["handle"], text)
    finally:
        with _LOCK:
            _WATCHES.pop(watch_id, None)


def _quiet_edit(handle, text: str) -> None:
    try:
        handle.edit(text)
    except Exception as exc:
        _LOGGER.debug("Babysit message edit failed (%s)", type(exc).__name__)


def _handle_event(state: dict, kind: str, line: str, now: float, throttle) -> None:
    event = f"{kind}: {line}".rstrip()
    text = f"{state['name']}: {event}\nElapsed: {now - state['started']:.0f}s | Lines seen: {state['lines']}"
    if throttle.should_edit(text, now):
        _quiet_edit(state["handle"], text)
    message = f"[babysit] {kind} in {state['name']}: {line}".rstrip()
    try:
        accepted = state["ctx"].inject_message(message, session_key=state["session_key"])
    except Exception as exc:
        _LOGGER.debug("Babysit session injection failed (%s)", type(exc).__name__)
        accepted = False
    if not accepted:
        try:
            state["route_context"].run(state["ctx"].post_message, message)
        except Exception as exc:
            _LOGGER.debug("Babysit context post failed (%s)", type(exc).__name__)


def _watch(args: dict, *, ctx) -> str:
    reserved = False
    try:
        path = _safe_log_path(str(args.get("log_path", "")))
        pid = args.get("pid")
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
            raise ValueError("pid must be a positive integer")
        stall_minutes = float(args.get("stall_minutes", 10))
        if not 0 < stall_minutes < float("inf"):
            raise ValueError("stall_minutes must be positive")
        progress_regex = args.get("progress_regex")
        if progress_regex is not None:
            re.compile(progress_regex)
        alert_patterns = args.get("alert_patterns") or []
        if not isinstance(alert_patterns, list):
            raise ValueError("alert_patterns must be a list of regular expressions")
        for pattern in alert_patterns:
            re.compile(pattern)
        global _STARTING
        with _LOCK:
            if len(_WATCHES) + _STARTING >= _MAX_WATCHES:
                raise ValueError("maximum of 8 active watches reached")
            _STARTING += 1
            reserved = True
        from gateway.session_context import get_session_env

        session_key = get_session_env("HERMES_SESSION_KEY")
        route_context = contextvars.copy_context()
        name = path.name
        handle = ctx.post_message(f"Watching {name}: waiting for log output")
        watch_id = uuid.uuid4().hex[:12]
        state = {
            "ctx": ctx, "path": path, "pid": pid, "name": name, "handle": handle,
            "session_key": session_key, "route_context": route_context,
            "job": watcher.JobWatch(progress_regex, stall_minutes * 60, alert_patterns),
            "started": time.monotonic(), "last_line": "", "lines": 0,
            "exit_seen": False, "stop": threading.Event(),
        }
        thread = threading.Thread(target=_watch_loop, args=(watch_id, state), daemon=True,
                                  name=f"babysit-{watch_id}")
        state["thread"] = thread
        with _LOCK:
            _WATCHES[watch_id] = state
            _STARTING -= 1
            reserved = False
        thread.start()
        return json.dumps({"ok": True, "watch_id": watch_id})
    except Exception as exc:
        if reserved:
            with _LOCK:
                _STARTING -= 1
        return json.dumps({"ok": False, "error": str(exc)})


def _stop(args: dict, **kwargs) -> str:
    watch_id = str(args.get("watch_id", ""))
    with _LOCK:
        state = _WATCHES.get(watch_id)
        if state is not None:
            state["stop"].set()
    return json.dumps({"ok": state is not None, "watch_id": watch_id})


def _list(args: dict | None = None, **kwargs) -> str:
    with _LOCK:
        watches = [
            {"watch_id": key, "name": item["name"], "pid": item["pid"],
             "lines": item["lines"], "status": item["last_line"]}
            for key, item in _WATCHES.items()
        ]
    return json.dumps({"ok": True, "watches": watches})


def _command(ctx, raw_args: str) -> str:
    return _list()


def _cleanup() -> None:
    with _LOCK:
        for state in _WATCHES.values():
            state["stop"].set()


def register(ctx) -> None:
    ctx.register_tool(
        name="babysit_watch", toolset="babysit", schema={
            "type": "object", "properties": {
                "log_path": {"type": "string", "description": "Path to an existing job log file."},
                "pid": {"type": ["integer", "null"], "description": "Optional process id to monitor."},
                "progress_regex": {"type": ["string", "null"], "description": "Optional progress line regex."},
                "stall_minutes": {"type": "number", "default": 10},
                "alert_patterns": {"type": "array", "items": {"type": "string"}, "default": []},
            }, "required": ["log_path"],
        }, handler=lambda args, **kwargs: _watch(args, ctx=ctx),
        description="Watch a job log and wake this session when it stalls or reports an issue.",
    )
    ctx.register_tool(name="babysit_stop", toolset="babysit", schema={
        "type": "object", "properties": {"watch_id": {"type": "string"}},
        "required": ["watch_id"],
    }, handler=_stop, description="Stop an active babysit watch.")
    ctx.register_tool(name="babysit_list", toolset="babysit", schema={
        "type": "object", "properties": {},
    }, handler=_list, description="List active babysit watches.")
    ctx.register_command("babysit", lambda raw: _command(ctx, raw),
                         description="List active job watches")
    ctx.on_unload(_cleanup)
