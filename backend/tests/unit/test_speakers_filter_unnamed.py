"""Issue #743 — the speaker filter is empty until a human renames someone.

``GET /speakers?for_filter=true`` only ever offered speakers a **person** had
given a real name to: the roster's base predicate required a non-empty
``display_name`` that did not look like a raw diarization label. On a library
where diarization ran and nobody renamed anyone — the state every library is in
right after its first upload — the endpoint answered ``[]`` and the gallery
sidebar showed nothing to filter by.

The predicate is not simply wrong, which is why it is not simply deleted. The
roster **groups by display name across files**, and ``SPEAKER_00`` in one
recording is a different person from ``SPEAKER_00`` in another; folding them
into one entry labelled like a person would be a worse lie than the empty list.
So unlabeled speakers are opt-in (``include_unnamed=true``), returned under
their diarization label, and **flagged** ``is_unnamed`` so the caller can never
render them as people. The sidebar uses them as one aggregate
"files with unlabeled speakers" facet, not as a person picker.

The last two tests are the other half of the issue — that *filtering* by what
the picker offers actually returns the right files, for both a named speaker
and an unlabeled diarization label.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

pytestmark = pytest.mark.unit


def _make_file(db, user, *, title="Recording"):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
        is_quarantined=False,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _add_speaker(db, media_file, owner, *, name, display_name=None):
    """Create a speaker the way diarization does — ``display_name`` is NULL
    until a human types one (``tasks/transcription/speaker_processor.py``)."""
    from app.models.media import Speaker

    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        media_file_id=media_file.id,
        name=name,
        display_name=display_name,
        verified=display_name is not None,
    )
    db.add(speaker)
    db.commit()
    db.refresh(speaker)
    return speaker


def _labels(payload) -> set[str]:
    """The string the picker actually offers, and the sidebar sends back."""
    return {row["display_name"] or row["name"] for row in payload}


def _by_label(payload) -> dict:
    return {row["display_name"] or row["name"]: row for row in payload}


def test_a_diarized_but_unnamed_library_gets_an_empty_roster_by_default(
    client, db_session, normal_user, user_token_headers
):
    """The reported symptom, pinned: without the opt-in the roster is still
    empty, because grouping unlabeled speakers by name across files is the
    thing the default must not do."""
    media = _make_file(db_session, normal_user, title="Standup")
    _add_speaker(db_session, media, normal_user, name="SPEAKER_00")
    _add_speaker(db_session, media, normal_user, name="SPEAKER_01")

    response = client.get("/api/speakers", params={"for_filter": True}, headers=user_token_headers)

    assert response.status_code == 200
    assert response.json() == []


def test_include_unnamed_offers_the_unlabeled_diarization_labels(
    client, db_session, normal_user, user_token_headers
):
    media = _make_file(db_session, normal_user, title="Standup")
    _add_speaker(db_session, media, normal_user, name="SPEAKER_00")
    _add_speaker(db_session, media, normal_user, name="SPEAKER_01")

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "include_unnamed": True},
        headers=user_token_headers,
    )

    assert response.status_code == 200
    assert _labels(response.json()) == {"SPEAKER_00", "SPEAKER_01"}


def test_every_unlabeled_entry_is_flagged_so_it_cannot_be_rendered_as_a_person(
    client, db_session, normal_user, user_token_headers
):
    media = _make_file(db_session, normal_user, title="Standup")
    _add_speaker(db_session, media, normal_user, name="SPEAKER_00")

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "include_unnamed": True},
        headers=user_token_headers,
    )

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["is_unnamed"] is True


def test_a_named_speaker_is_not_flagged_unnamed_and_is_ranked_first(
    client, db_session, normal_user, user_token_headers
):
    """A real name outranks a diarization label however common the label is —
    the picker must not bury the only person in the library under SPEAKER_00."""
    for i in range(3):
        media = _make_file(db_session, normal_user, title=f"Standup {i}")
        _add_speaker(db_session, media, normal_user, name="SPEAKER_00")
    named = _make_file(db_session, normal_user, title="Interview")
    _add_speaker(db_session, named, normal_user, name="SPEAKER_00", display_name="Priya Patel")

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "include_unnamed": True},
        headers=user_token_headers,
    )

    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["display_name"] == "Priya Patel"
    assert rows[0]["is_unnamed"] is False
    assert _by_label(rows)["SPEAKER_00"]["is_unnamed"] is True
    assert _by_label(rows)["SPEAKER_00"]["media_count"] == 3


def test_the_empty_string_display_name_the_pipeline_actually_writes_is_unlabeled(
    client, db_session, normal_user, user_token_headers
):
    """Reproduced against a live stack: a real transcription leaves
    ``display_name=''`` (not NULL, and not ``'SPEAKER_00'``) with the label in
    ``name``. So it is the ``display_name != ""`` predicate — not the
    ``^SPEAKER_\\d+$`` one — that empties the picker in the common case, and an
    empty string must group under the file-scoped label rather than collapsing
    every unnamed speaker in the library into one blank entry."""
    media = _make_file(db_session, normal_user, title="Standup")
    _add_speaker(db_session, media, normal_user, name="SPEAKER_00", display_name="")
    _add_speaker(db_session, media, normal_user, name="SPEAKER_01", display_name="")

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "include_unnamed": True},
        headers=user_token_headers,
    )

    assert response.status_code == 200
    rows = response.json()
    assert _labels(rows) == {"SPEAKER_00", "SPEAKER_01"}
    assert all(row["is_unnamed"] is True for row in rows)


def test_a_display_name_that_is_only_a_diarization_label_counts_as_unlabeled(
    client, db_session, normal_user, user_token_headers
):
    """``display_name='SPEAKER_02'`` is what an auto-label pass writes, not a
    person — the original predicate excluded it for exactly this reason and
    the opt-in path must keep classifying it as unlabeled."""
    media = _make_file(db_session, normal_user, title="Standup")
    _add_speaker(db_session, media, normal_user, name="SPEAKER_02", display_name="SPEAKER_02")

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "include_unnamed": True},
        headers=user_token_headers,
    )

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["is_unnamed"] is True


def test_q_searches_the_offered_label_not_just_the_display_name(
    client, db_session, normal_user, user_token_headers
):
    media = _make_file(db_session, normal_user, title="Standup")
    _add_speaker(db_session, media, normal_user, name="SPEAKER_00")
    _add_speaker(db_session, media, normal_user, name="SPEAKER_01")

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "include_unnamed": True, "q": "er_01"},
        headers=user_token_headers,
    )

    assert response.status_code == 200
    assert _labels(response.json()) == {"SPEAKER_01"}


def test_unlabeled_speakers_of_an_unshared_file_are_not_offered(
    client, db_session, normal_user, other_user, user_token_headers
):
    """The opt-in widens what is *named*, never who can see it."""
    theirs = _make_file(db_session, other_user, title="Not shared")
    _add_speaker(db_session, theirs, other_user, name="SPEAKER_07")

    response = client.get(
        "/api/speakers",
        params={"for_filter": True, "include_unnamed": True},
        headers=user_token_headers,
    )

    assert response.status_code == 200
    assert "SPEAKER_07" not in _labels(response.json())


def test_filtering_files_by_a_named_speaker_returns_only_that_speakers_files(
    client, db_session, normal_user, user_token_headers
):
    hers = _make_file(db_session, normal_user, title="Priya interview")
    _add_speaker(db_session, hers, normal_user, name="SPEAKER_00", display_name="Priya Patel")
    other = _make_file(db_session, normal_user, title="Quinn interview")
    _add_speaker(db_session, other, normal_user, name="SPEAKER_00", display_name="Quinn Zhao")

    response = client.get(
        "/api/files", params={"speaker": "Priya Patel"}, headers=user_token_headers
    )

    assert response.status_code == 200
    titles = {f["title"] for f in response.json()["items"]}
    assert titles == {"Priya interview"}


def test_filtering_files_by_an_unlabeled_label_returns_the_files_carrying_it(
    client, db_session, normal_user, user_token_headers
):
    """What the aggregate "unlabeled speakers" facet resolves to on the wire:
    ``?speaker=SPEAKER_01`` matches ``Speaker.name``, so a file whose
    diarization only found one speaker is correctly excluded."""
    two_speakers = _make_file(db_session, normal_user, title="Standup")
    _add_speaker(db_session, two_speakers, normal_user, name="SPEAKER_00")
    _add_speaker(db_session, two_speakers, normal_user, name="SPEAKER_01")
    one_speaker = _make_file(db_session, normal_user, title="Monologue")
    _add_speaker(db_session, one_speaker, normal_user, name="SPEAKER_00")

    response = client.get(
        "/api/files", params={"speaker": "SPEAKER_01"}, headers=user_token_headers
    )

    assert response.status_code == 200
    titles = {f["title"] for f in response.json()["items"]}
    assert titles == {"Standup"}
