"""Small time and duration formatters used by the runner and the reports."""

from __future__ import annotations

from datetime import datetime


def now_ts() -> str:
    return datetime.now().strftime('%H:%M:%S')


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f'{seconds:.1f}s'
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f'{m}m{s:02d}s'
    h, m = divmod(m, 60)
    return f'{h}h{m:02d}m{s:02d}s'
