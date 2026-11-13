"""E2E: the processing card still reaches "Complete" when every post-processing frame is lost (#1308 item C).

The card folds ``post_processing_status`` WebSocket frames; when it cannot tell what
post-processing is doing it reads ``GET /api/files/{uuid}/post-processing`` instead
(``frontend/src/lib/notifications/reconcile.ts``). This drives that fallback in a real browser
against REAL workers:

* a committed clip is uploaded and processed to completion, and the card is seeded for it
  (the same ``notifications`` localStorage record the app persists);
* the ``/api/ws`` socket is routed with Playwright's ``Page.route_web_socket``
  (https://playwright.dev/python/docs/api/class-page#page-route-web-socket) connected to the
  real server, with a ``server.on_message`` relay (``ws_frames.FrameFilter``, pinned by
  ``unit/test_e2e_ws_frame_filter.py``);
* a real analytics re-run is requested, so the workers themselves emit the
  ``post_processing_status`` frames, and the snapshot is whatever the real server answers.

Two runs share the body. The DROP run withholds the frames and asserts that real frames were
withheld, that none reached the page, and that the card completed from a snapshot the server
served. The CONTROL run keeps the frames and asserts that the same re-run does put
``post_processing_status`` frames on the wire. Without the control, a drop run in which no
frame was ever sent would pass for the wrong reason.
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
from typing import TypedDict

import pytest
import requests
from page_state import PageState
from playwright.sync_api import Page
from playwright.sync_api import Response
from playwright.sync_api import WebSocketRoute
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS
from ws_frames import FrameFilter

pytestmark = pytest.mark.upload

POST_PROCESSING_FRAME = "post_processing_status"


class _LostFramesState(TypedDict):
    """What the fixture hands its test: the page's socket, filter and observed snapshots."""

    file_id: str
    filename: str
    filter: FrameFilter
    ws: WebSocketRoute | None
    snapshots: list[dict[str, Any]]
    #: Every post-processing read the page made, `(status, body_read)`, for the failure message.
    reads: list[tuple[int, bool]]


_LOST_FRAMES: PageState[_LostFramesState] = PageState()


def _open_page(
    browser: Any,
    shared_auth_state: str,
    base_url: str,
    media: dict[str, Any],
    drop_types: tuple[str, ...],
) -> tuple[Page, Any]:
    file_id = str(media["uuid"])
    filename = str(media["filename"])
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
    state: _LostFramesState = {
        "file_id": file_id,
        "filename": filename,
        "filter": FrameFilter(drop_types=drop_types),
        "ws": None,
        "snapshots": [],
        "reads": [],
    }
    _LOST_FRAMES.attach(page, state)

    def on_socket(ws: WebSocketRoute) -> None:
        server = ws.connect_to_server()
        server.on_message(lambda message: state["filter"].relay(message, ws.send))
        state["ws"] = ws

    def on_response(response: Response) -> None:
        if not response.url.endswith(f"/api/files/{file_id}/post-processing"):
            return
        body_read = False
        if response.ok:
            with contextlib.suppress(Exception):
                state["snapshots"].append(response.json())
                body_read = True
        state["reads"].append((response.status, body_read))

    page.route_web_socket("**/api/ws", on_socket)
    page.on("response", on_response)
    page.goto(base_url)
    page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
    return page, context


@pytest.fixture
def real_media(owned_media_factory: Any, admin_token: str) -> dict[str, Any]:
    return dict(owned_media_factory(admin_token))


def _rerun_analytics(backend_url: str, admin_token: str, file_id: str) -> None:
    resp = requests.post(
        f"{backend_url}/api/files/{file_id}/reprocess",
        json={"stages": ["analytics"]},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=60,
    )
    assert resp.status_code == 200, f"reprocess refused: {resp.status_code} {resp.text[:300]}"


def _wait_for(page: Page, condition: Any, what: str, seconds: float = 90) -> None:
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        page.wait_for_timeout(250)
    assert condition(), what


def _drive(
    browser: Any,
    shared_auth_state: str,
    base_url: str,
    backend_url: str,
    admin_token: str,
    media: dict[str, Any],
    drop_types: tuple[str, ...],
) -> tuple[Page, _LostFramesState]:
    page, context = _open_page(browser, shared_auth_state, base_url, media, drop_types)
    state = _LOST_FRAMES.of(page)
    try:
        page.locator(".notifications-btn").click()
        panel = page.locator(".notifications-panel")
        expect(panel).to_be_visible(timeout=15000)
        card = panel.locator(".processing-job", has_text=state["filename"])
        expect(card).to_have_count(1, timeout=15000)
        _wait_for(page, lambda: state["ws"] is not None, "the page never opened /api/ws")

        _rerun_analytics(backend_url, admin_token, state["file_id"])
        fltr = state["filter"]
        if drop_types:
            _wait_for(
                page,
                lambda: bool(fltr.dropped),
                "the workers sent no post_processing_status frame to drop",
            )
        else:
            _wait_for(
                page,
                lambda: any(fltr.frame_type(r) == POST_PROCESSING_FRAME for r in fltr.relayed),
                "the control run saw no post_processing_status frame on the wire",
            )
        expect(card.locator(".job-state")).to_have_text("Complete", timeout=120000)
        if drop_types:
            # The card completes the moment the page applies the snapshot; the handler that
            # records that response reads its body asynchronously and can still be in flight.
            _wait_for(
                page,
                lambda: bool(state["snapshots"]),
                "the card never read GET /files/{uuid}/post-processing "
                f"(reads seen, status and body read: {state['reads']})",
                seconds=15,
            )
        return page, state
    except BaseException:
        page.close()
        context.close()
        raise


class TestLostPostProcessingFrames:
    def test_card_completes_from_the_snapshot_when_every_frame_is_dropped(
        self,
        browser: Any,
        shared_auth_state: str,
        base_url: str,
        backend_url: str,
        admin_token: str,
        real_media: dict[str, Any],
    ) -> None:
        page, state = _drive(
            browser,
            shared_auth_state,
            base_url,
            backend_url,
            admin_token,
            real_media,
            (POST_PROCESSING_FRAME,),
        )
        fltr = state["filter"]
        leaked = [r for r in fltr.relayed if fltr.frame_type(r) == POST_PROCESSING_FRAME]
        assert fltr.dropped and leaked == [], (
            f"post_processing_status frames reached the page: {leaked}"
        )
        # The card could only have completed from a snapshot the real server served.
        assert state["snapshots"], "the card never read GET /files/{uuid}/post-processing"
        assert state["snapshots"][-1].get("tasks") or {}, state["snapshots"][-1]
        page.context.close()

    def test_control_run_keeping_the_frames_sees_them_on_the_wire(
        self,
        browser: Any,
        shared_auth_state: str,
        base_url: str,
        backend_url: str,
        admin_token: str,
        real_media: dict[str, Any],
    ) -> None:
        """Same re-run, nothing dropped: proves the drop run's frames are real, so it can fail."""
        page, state = _drive(
            browser,
            shared_auth_state,
            base_url,
            backend_url,
            admin_token,
            real_media,
            (),
        )
        fltr = state["filter"]
        frames = [r for r in fltr.relayed if fltr.frame_type(r) == POST_PROCESSING_FRAME]
        assert frames and not fltr.dropped, (
            f"expected post_processing_status frames relayed: relayed={len(frames)}"
        )
        page.context.close()
