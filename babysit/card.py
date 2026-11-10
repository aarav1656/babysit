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


def _metrics_line(store: MetricStore) -> str:
    keys = [key for key, points in store.series.items() if points]
    lead = primary_key(keys)
    parts = []
    if lead:
        last = store.series[lead][-1][2]
        parts.append(" ".join(filter(None, (lead, fmt_num(last), sparkline(store.values(lead, _SPARK_WINDOW))))))
    if "lr" in store.series:
        parts.append(f"lr {fmt_num(store.series['lr'][-1][2])}")
    extra = next((key for key in _EXTRA if key in store.series and key != lead), None)
    if extra:
        parts.append(f"{extra} {fmt_num(store.series[extra][-1][2])}")
    if rate := store.rate():
        parts.append(f"{rate:.2g} it/s" if rate >= 0.1 else f"{1 / rate:.2g} s/it")
    return "  ".join(parts)


def _progress_line(store: MetricStore) -> str:
    if store.frac is None:
        return ""
    eta = store.eta()
    done = store.frac >= 1
    return f"{bar(store.frac)} " + ("done" if done else f"ETA {fmt_duration(eta)}" if eta is not None else "ETA ?")


def render_card(name: str, store: MetricStore, last: str, elapsed: float, lines: int,
                event: tuple[str, str] | None = None) -> str:
    """Up to four lines: a headline (the event and its marker when one fired), the bar, the metrics, the footer.

    A log with no step or percentage keeps the plain ``name: last progress line`` headline."""
    where, progress = _where(store), _progress_line(store)
    if event is not None:
        kind, text = event
        head = f"{_MARKERS.get(kind, '■')} {name}: {kind}: {text}".rstrip()
        progress = f"{progress}  {where}".strip() if progress else where
    else:
        head = f"{name}  {where}" if where else f"{name}: {last}".rstrip()
    footer = f"Elapsed: {elapsed:.0f}s | Lines seen: {lines}"
    return "\n".join(row for row in (head, progress, _metrics_line(store), footer) if row)
