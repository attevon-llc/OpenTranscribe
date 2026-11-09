"""/metrics must not re-render the whole page for every scrape inside the TTL."""

import pytest

from app.api.endpoints import metrics as m


@pytest.fixture
def page(monkeypatch):
    """Stub every collector refresh; ``renders`` records each full-page render."""
    monkeypatch.setattr(m, "_page", None)
    for name in (
        "update_queue_depths",
        "update_transcription_lease_metrics",
        "refresh_job_metrics",
        "refresh_stuck_gauge",
    ):
        monkeypatch.setattr(m, name, lambda: None)
    renders: list[bytes] = []

    def render() -> bytes:
        renders.append(b"x")
        return b"render-%d\n" % len(renders)

    monkeypatch.setattr(m.metrics_multiprocess, "render_all", render)
    return renders


def test_scrapes_inside_ttl_render_once(page):
    bodies = [m.metrics().body for _ in range(5)]

    assert bodies == [b"render-1\n"] * 5
    assert len(page) == 1


def test_scrape_after_ttl_renders_again(page, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: now[0])

    first = m.metrics().body
    now[0] += m.METRICS_PAGE_TTL_SECONDS + 1
    second = m.metrics().body

    assert (first, second) == (b"render-1\n", b"render-2\n")
