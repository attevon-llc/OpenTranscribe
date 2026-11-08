"""E2E: the processing card still reaches "Complete" when every post-processing frame is lost (#1308 item C).

The card folds ``post_processing_status`` WebSocket frames; when it cannot tell what
post-processing is doing it reads ``GET /api/files/{uuid}/post-processing`` instead
(``frontend/src/lib/notifications/reconcile.ts``). This drives that fallback in a real browser:

* the ``/api/ws`` socket is routed with Playwright's ``Page.route_web_socket``
  (https://playwright.dev/python/docs/api/class-page#page-route-web-socket) connected to the
  real server, with a ``server.on_message`` relay that withholds ``post_processing_status``
  frames (``ws_frames.FrameFilter``, pinned by ``unit/test_e2e_ws_frame_filter.py``);
* the file is fictional, so the test pushes the frames the backend would have sent through that
  same relay and asserts they were withheld (``dropped`` is non-empty, ``relayed`` is empty).
  A relay that lets them through would complete the card by frame and never read the snapshot;
* the snapshot request is parked, so the card is observed "Finishing up" first, then released.

No worker runs and nothing is created in the library.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import Route
from playwright.sync_api import WebSocketRoute
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS
from ws_frames import FrameFilter

pytestmark = pytest.mark.upload


def _frame(kind: str, file_id: str, **data: Any) -> str:
    return json.dumps({"type": kind, "data": {"file_id": file_id, **data}})


@pytest.fixture
def lost_frames_page(browser: Any, shared_auth_state: str, base_url: str) -> Any:
    file_id = str(uuid.uuid4())
    filename = f"e2e-missed-frames-{uuid.uuid4().hex[:6]}.wav"
    # Transcript finished 45 s ago: past the card's reconcile threshold (30 s) and inside its
    # grace for a plan that never arrived (2 min), so the card is "Finishing up" and asks.
    finished = datetime.now(UTC) - timedelta(seconds=45)
    record = {
        "id": uuid.uuid4().hex,
        "type": "transcription_status",
        "title": "Transcription",
        "message": f"completed {filename}",
        "timestamp": finished.isoformat(),
        "read": False,
        "data": {
            "file_id": file_id,
            "filename": filename,
            "status": "completed",
            "progress": 100,
            "stage": "finalize",
        },
    }
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    context.add_init_script(
        f"localStorage.setItem('notifications', {json.dumps(json.dumps([record]))});"
    )
    page = context.new_page()
    state: dict[str, Any] = {
        "file_id": file_id,
        "filename": filename,
        "filter": FrameFilter(),
        "ws": None,
        "held": [],
    }
    page._lost = state  # type: ignore[attr-defined]

    def on_socket(ws: WebSocketRoute) -> None:
        server = ws.connect_to_server()
        server.on_message(lambda message: state["filter"].relay(message, ws.send))
        state["ws"] = ws

    page.route_web_socket("**/api/ws", on_socket)
    page.route(f"**/api/files/{file_id}/post-processing", lambda r: state["held"].append(r))
    page.goto(base_url)
    page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
    yield page
    for route in state["held"]:
        with contextlib.suppress(PlaywrightError):
            route.abort()
    page.close()
    context.close()


def _snapshot(file_id: str) -> dict[str, Any]:
    now = time.time()
    return {
        "file_id": file_id,
        "run_at": now - 40,
        "tasks": {
            "search_indexing": {"state": "done", "at": now - 30, "started_at": now - 35},
            "analytics": {"state": "done", "at": now - 28, "started_at": now - 33},
            "summary": {"state": "skipped", "at": now - 20},
        },
    }


class TestLostPostProcessingFrames:
    def test_card_completes_from_the_snapshot_when_every_frame_is_dropped(
        self, lost_frames_page: Page
    ) -> None:
        page = lost_frames_page
        state = page._lost  # type: ignore[attr-defined]
        file_id, filename = state["file_id"], state["filename"]
        fltr: FrameFilter = state["filter"]

        page.locator(".notifications-btn").click()
        panel = page.locator(".notifications-panel")
        expect(panel).to_be_visible(timeout=15000)
        card = panel.locator(".processing-job", has_text=filename)
        expect(card).to_have_count(1, timeout=15000)
        expect(card.locator(".job-state")).to_have_text("Finishing up")

        # The socket is up and routed; the card has asked the server for the run (parked).
        deadline = time.time() + 15
        while (state["ws"] is None or not state["held"]) and time.time() < deadline:
            page.wait_for_timeout(250)
        assert state["ws"] is not None, "the page never opened /api/ws through the route"
        assert state["held"], "the card never read GET /files/{uuid}/post-processing"

        # The frames the backend would have pushed for this file: a plan, then completion.
        now = time.time()
        plan = _frame(
            "post_processing_status",
            file_id,
            run_at=now - 40,
            tasks={"search_indexing": {"state": "queued", "at": now - 40}},
        )
        done = _frame(
            "post_processing_status",
            file_id,
            tasks={"search_indexing": {"state": "done", "at": now - 30}},
        )
        for raw in (plan, done):
            fltr.relay(raw, state["ws"].send)
        # The server also relays unrelated frames (e.g. connection_established); only
        # post-processing frames must have been withheld.
        leaked = [r for r in fltr.relayed if fltr.frame_type(r) == "post_processing_status"]
        assert len(fltr.dropped) == 2 and leaked == [], (
            f"post_processing_status frames reached the page: relayed={leaked}"
        )

        # Nothing has completed the card by frame...
        expect(card.locator(".job-state")).to_have_text("Finishing up")
        # ...so the snapshot alone must.
        held: Route = state["held"][0]
        held.fulfill(json=_snapshot(file_id))
        expect(card.locator(".job-state")).to_have_text("Complete", timeout=15000)
