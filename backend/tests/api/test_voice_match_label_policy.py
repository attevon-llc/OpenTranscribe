"""A voiceprint-matched NAME must not surface while voiceprints are off (#1280, plan D5).

A speaker row can carry an unverified ``voice_match`` suggestion ("Zylofenix Quorra", 0.91)
that the canonical label promotes to the speaker's name. The speaker endpoints already
withhold such a suggestion while the owner's scope has voiceprints off; this suite holds the
OTHER places the same name travels to the same rule: the file detail and segment pages
(``resolved_display_name``, ``status_text``, ``resolved_speaker_name``) and every download
that prints a speaker label (txt, json, csv, md, srt, vtt, the player captions, the subtitle
download).

Every case has an "on" control that shows the name, and a human-typed name stays visible
while the switch is off, so "always hide" cannot pass.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.models.media import TranscriptSegment
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")

MATCHED = "Zylofenix Quorra"
TYPED = "Quillon Brackett"
FILE_STATUS = "completed"


@pytest.fixture
def world(db_session, normal_user):
    """One file, two speakers: a voiceprint-matched unverified name and a human-typed one."""
    file = make_file(db_session, normal_user)
    matched = make_speaker(
        db_session,
        normal_user,
        file,
        name="SPEAKER_00",
        suggested_name=MATCHED,
        suggestion_source="voice_match",
        confidence=0.91,
        verified=False,
    )
    typed = make_speaker(
        db_session,
        normal_user,
        file,
        name="SPEAKER_01",
        display_name=TYPED,
        verified=False,
    )
    for i, speaker in enumerate((matched, typed)):
        db_session.add(
            TranscriptSegment(
                media_file_id=file.id,
                speaker_id=speaker.id,
                start_time=float(i),
                end_time=float(i) + 0.9,
                text=f"segment number {i}",
            )
        )
    db_session.commit()
    return {"file": file, "matched": matched, "typed": typed}


def _off(db_session) -> None:
    set_instance_switch(db_session, "voiceprints", False)


def _everything(value) -> str:
    return repr(value)


def test_file_detail_shows_the_matched_name_while_voiceprints_are_on(
    client, user_token_headers, world
):
    body = client.get(f"/api/files/{world['file'].uuid}", headers=user_token_headers).json()

    speakers = {s["name"]: s for s in body["speakers"]}
    assert speakers["SPEAKER_00"]["resolved_display_name"] == MATCHED
    assert "High confidence" in speakers["SPEAKER_00"]["status_text"]
    assert MATCHED in {s.get("resolved_speaker_name") for s in body["transcript_segments"]}


def test_file_detail_withholds_the_matched_name_while_voiceprints_are_off(
    client, user_token_headers, world, db_session
):
    _off(db_session)

    resp = client.get(f"/api/files/{world['file'].uuid}", headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert MATCHED not in _everything(body)
    speakers = {s["name"]: s for s in body["speakers"]}
    assert speakers["SPEAKER_00"]["resolved_display_name"] == "SPEAKER_00"
    assert "match" not in speakers["SPEAKER_00"]["status_text"].lower()
    # A name a person typed is theirs, not a voiceprint's.
    assert speakers["SPEAKER_01"]["resolved_display_name"] == TYPED
    segment_names = {s["speaker"]["name"]: s["speaker"] for s in body["transcript_segments"]}
    assert segment_names["SPEAKER_00"]["resolved_display_name"] == "SPEAKER_00"
    assert segment_names["SPEAKER_01"]["resolved_display_name"] == TYPED


def test_segments_page_withholds_the_matched_name_while_voiceprints_are_off(
    client, user_token_headers, world, db_session
):
    url = f"/api/files/{world['file'].uuid}/segments"
    assert MATCHED in _everything(client.get(url, headers=user_token_headers).json())

    _off(db_session)
    resp = client.get(url, headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert MATCHED not in _everything(resp.json())
    assert TYPED in _everything(resp.json())


@pytest.mark.parametrize("fmt", ["txt", "json", "csv", "md", "srt", "vtt"])
def test_transcript_export_prints_the_matched_name_only_while_voiceprints_are_on(
    client, user_token_headers, world, db_session, fmt
):
    url = f"/api/files/{world['file'].uuid}/export?format={fmt}"
    on = client.get(url, headers=user_token_headers)
    assert on.status_code == status.HTTP_200_OK, on.text
    assert MATCHED in on.text

    _off(db_session)
    off = client.get(url, headers=user_token_headers)

    assert off.status_code == status.HTTP_200_OK, off.text
    assert MATCHED not in off.text
    assert "SPEAKER_00" in off.text
    assert TYPED in off.text


def test_player_captions_print_the_matched_name_only_while_voiceprints_are_on(
    client, user_token_headers, world, db_session
):
    url = f"/api/files/{world['file'].uuid}/captions"
    assert MATCHED in client.get(url, headers=user_token_headers).text

    _off(db_session)
    off = client.get(url, headers=user_token_headers)

    assert off.status_code == status.HTTP_200_OK, off.text
    assert MATCHED not in off.text
    assert TYPED in off.text


@pytest.mark.parametrize("fmt", ["srt", "webvtt", "txt"])
def test_subtitle_download_prints_the_matched_name_only_while_voiceprints_are_on(
    client, user_token_headers, world, db_session, fmt
):
    url = f"/api/files/{world['file'].uuid}/subtitles?subtitle_format={fmt}"
    on = client.get(url, headers=user_token_headers)
    assert on.status_code == status.HTTP_200_OK, on.text
    assert MATCHED in on.text

    _off(db_session)
    off = client.get(url, headers=user_token_headers)

    assert off.status_code == status.HTTP_200_OK, off.text
    assert MATCHED not in off.text
    assert TYPED in off.text


# --- The non-HTTP planes that carry the same label -------------------------------------------


@pytest.mark.parametrize("fmt", ["docx", "pdf"])
def test_document_export_prints_the_matched_name_only_while_voiceprints_are_on(
    client, user_token_headers, world, db_session, document_export, fmt
):
    from fixtures.document_export import document_text

    on = document_text(
        fmt, document_export.export(client, user_token_headers, world["file"], format=fmt)
    )
    assert MATCHED in on

    _off(db_session)
    off = document_text(
        fmt, document_export.export(client, user_token_headers, world["file"], format=fmt)
    )

    assert MATCHED not in off
    assert TYPED in off


def test_the_search_index_labels_a_chunk_with_the_matched_name_only_while_voiceprints_are_on(
    db_session, world
):
    from app.tasks.reindex_task import _extract_file_metadata

    def indexed_speakers() -> set[str]:
        meta = _extract_file_metadata(db_session, world["file"])
        assert meta is not None
        return {str(s["speaker"]) for s in meta["segments"]}

    assert indexed_speakers() == {MATCHED, TYPED}

    _off(db_session)

    assert indexed_speakers() == {"SPEAKER_00", TYPED}


def test_the_live_indexer_labels_a_chunk_like_the_reindex(db_session, world):
    from app.tasks.search_indexing_task import resolve_chunk_speaker_name

    assert resolve_chunk_speaker_name(world["matched"], voiceprint_names=True) == MATCHED

    _off(db_session)

    assert resolve_chunk_speaker_name(world["matched"], voiceprint_names=False) == "SPEAKER_00"
    assert resolve_chunk_speaker_name(world["typed"], voiceprint_names=False) == TYPED


def test_chat_cannot_resolve_a_speaker_through_a_hidden_voice_match(db_session, normal_user, world):
    """The roster is what a 'who said X' question is resolved against (and what reaches the
    model): a voice-matched name must not answer while the file's voiceprints are off."""
    from app.services.chat.speaker_resolver import build_candidate_roster

    def names() -> set[str]:
        roster = build_candidate_roster(db_session, normal_user.id, [MATCHED, TYPED])
        return {entry.name for entry in roster.entries}

    assert {MATCHED, TYPED} <= names()

    _off(db_session)

    off = names()
    assert MATCHED not in off
    assert TYPED in off


def test_the_rename_propagation_plane_expects_the_label_the_index_holds(
    db_session, world, monkeypatch
):
    """A rename narrows its update to the label the index holds now; with the matched name
    withheld that is the placeholder, or the rewrite would match nothing and drift."""
    from contextlib import contextmanager

    from app.tasks import rename_propagation_task as rpt

    @contextmanager
    def same_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", same_session)
    assert rpt._current_speaker_name(int(world["matched"].id)) == MATCHED

    _off(db_session)

    assert rpt._current_speaker_name(int(world["matched"].id)) == "SPEAKER_00"


def test_a_label_that_is_decided_by_a_person_is_never_withheld(db_session, normal_user, world):
    """A verified voice match is a person's assertion, not a voiceprint's, and keeps its name."""
    world["matched"].verified = True
    db_session.commit()
    _off(db_session)

    from app.utils.speaker_labels import canonical_speaker_label_for_row

    assert canonical_speaker_label_for_row(world["matched"]) == MATCHED


def test_speaker_list_and_detail_show_the_placeholder_while_voiceprints_are_off(
    client, user_token_headers, world, db_session
):
    list_url = f"/api/speakers?file_uuid={world['file'].uuid}"
    detail_url = f"/api/speakers/{world['matched'].uuid}"
    assert MATCHED in repr(client.get(list_url, headers=user_token_headers).json())
    assert MATCHED in repr(client.get(detail_url, headers=user_token_headers).json())

    _off(db_session)
    listed = client.get(list_url, headers=user_token_headers)
    detail = client.get(detail_url, headers=user_token_headers)

    assert listed.status_code == status.HTTP_200_OK, listed.text
    assert detail.status_code == status.HTTP_200_OK, detail.text
    assert MATCHED not in listed.text
    assert MATCHED not in detail.text
