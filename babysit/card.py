"""The status card: progress, bar, ETA and a loss sparkline as plain text a chat message or a status line can carry."""

import math

from .metrics import MetricStore, primary_key

_BLOCKS = "▁▂▃▄▅▆▇█"
_PARTIAL = " ▏▎▍▌▋▊▉"
_MARKERS = {"nan": "✖", "oom": "✖", "traceback": "✖", "alert": "⚠", "stall": "⚠"}  # anything else: ■
_SPARK_WINDOW = 60  # newest samples the sparkline summarises
_EXTRA = ("val_loss", "val/loss", "acc", "accuracy", "val_acc", "grad_norm", "gnorm")


def sparkline(values: list[float], width: int = 10) -> str:
    """One block per bucket of the newest values, scaled between their own min and max."""
    values = [v for v in values if math.isfinite(v)]
    if not values:
        return ""
    if len(values) > width:
        edges = [round(i * len(values) / width) for i in range(width + 1)]
        values = [sum(values[a:b]) / (b - a) for a, b in zip(edges, edges[1:])]
    low, high = min(values), max(values)
    if high == low:
        return _BLOCKS[3] * len(values)
    return "".join(_BLOCKS[min(7, int((v - low) / (high - low) * 8))] for v in values)


def bar(frac: float, width: int = 20) -> str:
    """``▕████▍     ▏``: eighth-block resolution."""
    eighths = round(max(0.0, min(1.0, frac)) * width * 8)
    full, part = divmod(eighths, 8)
    body = "█" * full + (_PARTIAL[part] if part else "")
    return "▕" + body.ljust(width) + "▏"


def fmt_num(value: float) -> str:
    if value != 0 and abs(value) < 1e-3:
        mantissa, exponent = f"{value:.0e}".split("e")
        return f"{mantissa}e{int(exponent)}"
    text = f"{value:.3g}"
    if "e" in text:
        mantissa, exponent = text.split("e")
        return f"{mantissa}e{int(exponent)}"
    return text


def fmt_bytes(value: float) -> str:
    for unit in "BKMG":
        if value < 1024:
            return f"{value:.0f}{unit}" if value >= 10 or unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.0f}T" if value >= 10 else f"{value:.1f}T"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    return f"{hours // 24}d{hours % 24}h"


def _where(store: MetricStore) -> str:
    """``step 4120/10000  41%``, empty until the log shows a step or a percentage."""
    parts = []
    if store.step is not None:
        parts.append(f"step {store.step}/{store.total}" if store.total else f"step {store.step}")
    if store.frac is not None:
        parts.append(f"{store.frac * 100:.0f}%")
    return "  ".join(parts)


def _lead(store: MetricStore, width: int) -> str:
    """``loss 1.36 ▁▂▃▅``: the metric a chart leads with, its newest value and a sparkline."""
    lead = primary_key(key for key, points in store.series.items() if points)
    if not lead or lead == "rss_mb":
        return ""
    return " ".join(filter(None, (lead, fmt_num(store.series[lead][-1][2]), sparkline(store.values(lead, _SPARK_WINDOW), width))))


def _metrics_line(store: MetricStore, lead: bool = True) -> str:
    keys = [key for key, points in store.series.items() if points]
    parts = [_lead(store, 10)] if lead else []
    if "lr" in store.series:
        parts.append(f"lr {fmt_num(store.series['lr'][-1][2])}")
    extra = next((key for key in _EXTRA if key in store.series and key != primary_key(keys)), None)
    if extra:
        parts.append(f"{extra} {fmt_num(store.series[extra][-1][2])}")
    if rate := store.rate():
        parts.append(f"{rate:.2g} it/s" if rate >= 0.1 else f"{1 / rate:.2g} s/it")
    if store.mem:
        rss, limit = store.mem
        parts.append(f"mem {fmt_bytes(rss)} {rss / limit:.0%}")
    return "  ".join(part for part in parts if part)


def _eta(store: MetricStore) -> str:
    eta = store.eta()
    return "done" if store.frac is not None and store.frac >= 1 else f"ETA {fmt_duration(eta)}" if eta is not None else "ETA ?"


def _progress_line(store: MetricStore) -> str:
    return "" if store.frac is None else f"{bar(store.frac)} {_eta(store)}"


def _headline(name: str, store: MetricStore) -> str:
    """One line that carries the job's state alone, because some surfaces (the TUI status bar) show only the
    first line of a status message: ``train.log ▕████▏ 41%  step 4100/10000  loss 1.36 ▁▃▅  ETA 17m``."""
    parts = []
    if store.frac is not None:
        parts.append(f"{bar(store.frac, 10)} {store.frac * 100:.0f}%")
    if store.step is not None:
        parts.append(f"step {store.step}/{store.total}" if store.total else f"step {store.step}")
    if lead := _lead(store, 6):
        parts.append(lead)
    if store.frac is not None:
        parts.append(_eta(store))
    return f"{name} " + "  ".join(parts)


def render_card(name: str, store: MetricStore, last: str, elapsed: float, lines: int,
                event: tuple[str, str] | None = None, notes: str = "") -> str:
    """Up to five lines. Without an event the first line is the whole state (bar, step, loss, ETA) and the
    second the secondary metrics; with one, the event and its marker lead, then the progress bar and every metric.
    The caller's notes (model calls, wakes, stall limit) and the footer close it.

    A log with no step or percentage keeps the plain ``name: last progress line`` headline."""
    footer = f"Elapsed: {elapsed:.0f}s | Lines seen: {lines}"
    if event is None:
        if store.frac is None and store.step is None:
            return "\n".join(row for row in (f"{name}: {last}".rstrip(), _metrics_line(store), notes, footer) if row)
        return "\n".join(row for row in (_headline(name, store), _metrics_line(store, lead=False), notes, footer) if row)
    kind, text = event
    head = f"{_MARKERS.get(kind, '■')} {name}: {kind}: {text}".rstrip()
    where, progress = _where(store), _progress_line(store)
    progress = f"{progress}  {where}".strip() if progress else where
    return "\n".join(row for row in (head, progress, _metrics_line(store), notes, footer) if row)
