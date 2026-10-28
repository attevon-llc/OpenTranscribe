"""E2E: switching voiceprints off hides voice-made speaker names, and says why (#1335).

The shared ``owned_diarized_transcript_file`` carries only diarizer labels, so hiding a
name there would change nothing and the test could not fail. This module injects its OWN
transcript and then gives each speaker the identity a voiceprint match writes
(``identity_source='voiceprint'`` plus a verified ``display_name``), which is exactly the
row the privacy policy exists to hide. The control is the same file with voiceprints on.

Hiding is a view over unchanged data, so every test also switches the layer back on and
asserts the name returns; the layer is restored in a ``finally`` either way.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from conftest import OWNED_MEDIA_PREFIX
from owned_corpus import _inject_transcript
from playwright.sync_api import Page
from playwright.sync_api import expect
from privacy_ui import PAGE_READY_MS

pytestmark = pytest.mark.transcription

#: Distinctive, so it cannot be mistaken for any default label.
MATCHED_PREFIX = "Voicematch"
NOTICE = '[data-testid="voiceprints-off-notice"]'


@pytest.fixture
def voice_named_file(backend_url: str, admin_token: str) -> Iterator[dict[str, Any]]:
    """An owned transcript whose speakers carry voiceprint-written names."""
    from app.db.base import SessionLocal
    from app.models.media import Speaker

    suffix = uuid.uuid4().hex[:6]
    for descriptor in _inject_transcript(
        backend_url,
        admin_token,
        meeting_id=f"{OWNED_MEDIA_PREFIX}voicematch-{suffix}",
        title="E2E owned voice-matched transcript",
        turns=6,
    ):
        session = SessionLocal()
        try:
            rows = session.query(Speaker).filter(
                Speaker.media_file_id == descriptor["media_file_id"]
            )
            names: dict[str, str] = {}
            for index, row in enumerate(rows.order_by(Speaker.id)):
                machine_name = f"{MATCHED_PREFIX} {suffix} {index}"
                names[str(row.name)] = machine_name
                row.display_name = machine_name
                row.identity_source = "voiceprint"
                row.verified = True
            session.commit()
        finally:
            session.close()
        assert names, "the injected transcript produced no speaker rows to rename"
        yield {**descriptor, "matched_names": names}


def _put(backend_url: str, token: str, path: str, body: dict[str, Any]) -> None:
    response = requests.put(
        f"{backend_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=30,
    )
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text[:200]}"


def _get(backend_url: str, token: str, path: str) -> dict[str, Any]:
    response = requests.get(
        f"{backend_url}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30
    )
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text[:200]}"
    return dict(response.json())


def _labels(page: Page) -> list[str]:
    return [text.strip() for text in page.locator(".segment-speaker").all_text_contents()]


def _open_file(page: Page, base_url: str, file_uuid: str) -> None:
    page.goto(f"{base_url}/files/{file_uuid}")
    expect(page.locator(".transcript-segment").first).to_be_visible(timeout=PAGE_READY_MS)


@pytest.mark.parametrize("layer", ["user", "instance"])
def test_voiceprints_off_shows_the_diarizer_label_and_the_notice(
    layer: str,
    authenticated_page: Page,
    base_url: str,
    backend_url: str,
    admin_token: str,
    voice_named_file: dict[str, Any],
) -> None:
    page = authenticated_page
    matched = set(voice_named_file["matched_names"].values())
    diarizer = set(voice_named_file["matched_names"])
    if layer == "user":
        path, key = "/api/privacy/me", "voiceprint_matching_enabled"
    else:
        path, key = "/api/admin/privacy-policy", "voiceprint_matching_enabled"
    previous = bool(_get(backend_url, admin_token, path)[key])
    try:
        # Control: with voiceprints on the voice-made names are what the page shows.
        _put(backend_url, admin_token, path, {key: True})
        _open_file(page, base_url, voice_named_file["uuid"])
        shown = set(_labels(page))
        assert shown and shown <= matched, shown
        page.locator(".edit-speakers-button").click()
        expect(page.locator(".speaker-editor-container")).to_be_visible()
        expect(page.locator(NOTICE)).to_have_count(0)

        _put(backend_url, admin_token, path, {key: False})
        _open_file(page, base_url, voice_named_file["uuid"])
        shown = set(_labels(page))
        assert shown and shown <= diarizer, shown
        assert not (shown & matched)
        page.locator(".edit-speakers-button").click()
        notice = page.locator(NOTICE)
        expect(notice).to_be_visible()
        # The notice names WHO turned it off, so the person is not left guessing.
        expected = "deployment operator" if layer == "instance" else "You turned this off"
        expect(notice).to_contain_text(expected)

        _put(backend_url, admin_token, path, {key: True})
        _open_file(page, base_url, voice_named_file["uuid"])
        assert set(_labels(page)) <= matched
    finally:
        requests.put(
            f"{backend_url}{path}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={key: previous},
            timeout=30,
        )
