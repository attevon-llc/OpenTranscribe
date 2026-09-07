"""Three per-resource reads/writes that no test referenced (``audit-route-coverage.py``).

* ``PUT /api/files/{file_uuid}/transcript/segments/{segment_uuid}`` — the transcript
  editor's save. The **segment must belong to the file named in the path**, or one
  file's uuid becomes a way to edit another's transcript.
* ``GET /api/speakers/{speaker_uuid}/cross-media`` — "where else does this person
  appear", matched by profile when there is one and by display name otherwise.
* ``GET /api/tags/{tag_uuid}/files`` — what a tag *touches*, gated on the tag plane's
  **read** scope (``visible_to``) while every mutation stays on the narrow writable
  scope.

All three are ``get_current_active_user`` routes whose real gate is ownership, so the
authz negatives here are cross-account rather than role-based. Every row is created on
the savepoint-isolated ``db_session`` and rolls back; nothing here touches MinIO or
OpenSearch, and no external service is substituted.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import status

from app.models.media import FileTag
from app.models.media import Speaker
from app.models.media import Tag
from app.models.media import TranscriptSegment
from tests.user_owned_rows import make_media_file

FILES = "/api/files"
SPEAKERS = "/api/speakers"
TAGS = "/api/tags"

#: Never inserted. A literal, not ``uuid4()`` — a parametrize argument is evaluated at
#: import time and becomes part of the test id, so a random one would give each xdist
#: worker a different id and collection would fail.
ABSENT_UUID = "00000000-0000-4000-8000-00000000cafe"


def _make_segment(db_session, media_file, *, text: str = "original text") -> TranscriptSegment:
    segment = TranscriptSegment(
        media_file_id=media_file.id,
        start_time=1.0,
        end_time=2.5,
        text=text,
    )
    db_session.add(segment)
    db_session.commit()
    db_session.refresh(segment)
    return segment


def _make_speaker(db_session, user, media_file, *, name: str, display_name: str | None) -> Speaker:
    speaker = Speaker(
        user_id=user.id,
        media_file_id=media_file.id,
        name=name,
        display_name=display_name,
        verified=False,
        confidence=0.8,
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    return speaker


def _make_tag(db_session, owner_id: int | None) -> Tag:
    name = f"pytest-tag-{uuid_pkg.uuid4().hex[:8]}"
    tag = Tag(name=name, user_id=owner_id, normalized_name=name, source="manual")
    db_session.add(tag)
    db_session.commit()
    db_session.refresh(tag)
    return tag


def _attach(db_session, tag: Tag, media_file) -> None:
    db_session.add(FileTag(tag_id=tag.id, media_file_id=media_file.id, source="manual"))
    db_session.commit()


# ---------------------------------------------------------------------------
# PUT /files/{file_uuid}/transcript/segments/{segment_uuid}
# ---------------------------------------------------------------------------
def _segment_url(file_uuid, segment_uuid) -> str:
    return f"{FILES}/{file_uuid}/transcript/segments/{segment_uuid}"


def test_editing_a_segment_saves_the_text_and_echoes_the_file_uuid(
    client, db_session, user_token_headers, normal_user
):
    """The editor's save: new text persisted, and the response keyed by **uuids**.

    ``media_file_id`` in the response is the file's uuid, not its integer id — the
    editor uses it to confirm it saved into the file it has open, and an internal id
    leaking out here would also be an id-enumeration surface.
    """
    media_file = make_media_file(db_session, int(normal_user.id))
    segment = _make_segment(db_session, media_file)

    response = client.put(
        _segment_url(media_file.uuid, segment.uuid),
        headers=user_token_headers,
        json={"text": "corrected transcript text"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["text"] == "corrected transcript text"
    assert body["media_file_id"] == str(media_file.uuid)
    assert body["uuid"] == str(segment.uuid)
    db_session.expire_all()
    db_session.refresh(segment)
    assert segment.text == "corrected transcript text"


def test_editing_a_segment_can_move_its_timings(
    client, db_session, user_token_headers, normal_user
):
    """Timings are editable, and the display strings are rebuilt from the new values.

    ``formatted_timestamp`` / ``display_timestamp`` are ``M:SS`` (fat backend — the
    SPA renders them rather than reformatting), so they must follow the edit; a
    response echoing the pre-edit strings would leave the transcript list showing the
    old time next to the new audio position.
    """
    media_file = make_media_file(db_session, int(normal_user.id))
    segment = _make_segment(db_session, media_file)

    response = client.put(
        _segment_url(media_file.uuid, segment.uuid),
        headers=user_token_headers,
        json={"start_time": 125.4, "end_time": 130.0},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["start_time"] == 125.4
    assert body["end_time"] == 130.0
    assert body["formatted_timestamp"] == "2:05"
    assert body["display_timestamp"] == "2:05"


def test_editing_a_segment_cannot_rewrite_its_primary_key(
    client, db_session, user_token_headers, normal_user
):
    """Issue #722: ``id`` reached the ORM via a blind ``setattr`` loop and let a client
    move a segment's primary key to any id it chose, including one already in use
    (which then raised an unhandled ``UniqueViolation`` -> 500). The field is vestigial
    — the handler resolves the segment from the path ``segment_uuid`` — so it must be
    rejected outright rather than merely ignored-if-harmless.
    """
    media_file = make_media_file(db_session, int(normal_user.id))
    segment = _make_segment(db_session, media_file)
    original_id = segment.id

    response = client.put(
        _segment_url(media_file.uuid, segment.uuid),
        headers=user_token_headers,
        json={"id": original_id + 99000000, "text": "MUTATED"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    db_session.expire_all()
    db_session.refresh(segment)
    assert segment.id == original_id
    assert segment.text == "original text"


def test_editing_a_segment_rejects_a_speaker_id_on_this_route(
    client, db_session, user_token_headers, normal_user
):
    """Issue #722: a schema-valid ``speaker_id`` UUID reached the ORM's integer
    ``speaker_id`` FK via the same blind ``setattr`` loop and raised an unhandled
    ``DatatypeMismatch`` -> 500. Speaker reassignment has its own endpoint
    (``PUT /transcripts/segments/{uuid}/speaker``, which resolves the uuid to the
    integer FK correctly) — this route must reject the field, never 500 or silently
    write it.
    """
    media_file = make_media_file(db_session, int(normal_user.id))
    segment = _make_segment(db_session, media_file)

    response = client.put(
        _segment_url(media_file.uuid, segment.uuid),
        headers=user_token_headers,
        json={"speaker_id": str(uuid_pkg.uuid4())},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    db_session.expire_all()
    db_session.refresh(segment)
    assert segment.speaker_id is None


def test_a_segment_from_another_file_is_404(client, db_session, user_token_headers, normal_user):
    """The segment lookup is filtered by the file in the path, not just by uuid.

    Without that filter, an owner of ANY file could edit any segment whose uuid they
    learned, because the ownership check would pass on their own file while the write
    landed on someone else's transcript. This is the highest-value test in the file.
    """
    mine = make_media_file(db_session, int(normal_user.id))
    other = make_media_file(db_session, int(normal_user.id))
    foreign_segment = _make_segment(db_session, other, text="do not touch")

    response = client.put(
        _segment_url(mine.uuid, foreign_segment.uuid),
        headers=user_token_headers,
        json={"text": "rewritten"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    db_session.expire_all()
    db_session.refresh(foreign_segment)
    assert foreign_segment.text == "do not touch"


def test_editing_someone_elses_segment_is_403(
    client, db_session, other_user_auth_headers, normal_user
):
    """``require_resource_owner`` on the media file, before any segment work."""
    media_file = make_media_file(db_session, int(normal_user.id))
    segment = _make_segment(db_session, media_file, text="private words")

    response = client.put(
        _segment_url(media_file.uuid, segment.uuid),
        headers=other_user_auth_headers,
        json={"text": "rewritten"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    db_session.expire_all()
    db_session.refresh(segment)
    assert segment.text == "private words"


def test_editing_a_segment_of_an_unknown_file_is_404(client, user_token_headers):
    response = client.put(
        _segment_url(ABSENT_UUID, ABSENT_UUID), headers=user_token_headers, json={"text": "x"}
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_editing_a_segment_requires_authentication(client):
    response = client.put(_segment_url(ABSENT_UUID, ABSENT_UUID), json={"text": "x"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /speakers/{speaker_uuid}/cross-media
# ---------------------------------------------------------------------------
def test_cross_media_lists_the_speakers_own_file_first(
    client, db_session, user_token_headers, normal_user
):
    """With no profile and a diarization-style name, the answer is just this file.

    ``same_speaker`` is what the UI uses to separate "this recording" from "also
    appears in"; a bare list with the flag dropped would look identical in length.
    """
    media_file = make_media_file(db_session, int(normal_user.id))
    speaker = _make_speaker(
        db_session, normal_user, media_file, name="SPEAKER_01", display_name="SPEAKER_01"
    )

    response = client.get(f"{SPEAKERS}/{speaker.uuid}/cross-media", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert len(body) == 1
    assert body[0]["media_file_id"] == str(media_file.uuid)
    assert body[0]["same_speaker"] is True
    assert body[0]["speaker_label"] == "SPEAKER_01"


def test_cross_media_matches_other_files_by_display_name(
    client, db_session, user_token_headers, normal_user
):
    """A named speaker is matched across the caller's other recordings by that name.

    Catches the ``display_name`` arm being dropped (the panel would claim a named
    person appears nowhere else) and catches the ``SPEAKER_`` prefix guard being
    dropped, which is covered by its own case below.
    """
    first = make_media_file(db_session, int(normal_user.id))
    second = make_media_file(db_session, int(normal_user.id))
    speaker = _make_speaker(
        db_session, normal_user, first, name="SPEAKER_00", display_name="Dana Scully"
    )
    _make_speaker(db_session, normal_user, second, name="SPEAKER_03", display_name="Dana Scully")

    response = client.get(f"{SPEAKERS}/{speaker.uuid}/cross-media", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert {entry["media_file_id"] for entry in body} == {str(first.uuid), str(second.uuid)}
    # The queried speaker sorts first — same_speaker outranks confidence.
    assert body[0]["media_file_id"] == str(first.uuid)
    assert [entry["same_speaker"] for entry in body] == [True, False]


def test_cross_media_does_not_match_a_diarization_placeholder_name(
    client, db_session, user_token_headers, normal_user
):
    """``SPEAKER_01`` in two files is two unrelated people, not one appearing twice.

    Without the prefix guard every recording's first speaker would be reported as the
    same person, which is exactly the false cross-media match the panel exists to
    help an operator judge.
    """
    first = make_media_file(db_session, int(normal_user.id))
    second = make_media_file(db_session, int(normal_user.id))
    speaker = _make_speaker(
        db_session, normal_user, first, name="SPEAKER_01", display_name="SPEAKER_01"
    )
    _make_speaker(db_session, normal_user, second, name="SPEAKER_01", display_name="SPEAKER_01")

    response = client.get(f"{SPEAKERS}/{speaker.uuid}/cross-media", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert [entry["media_file_id"] for entry in response.json()] == [str(first.uuid)]


def test_cross_media_excludes_another_users_speaker_of_the_same_name(
    client, db_session, user_token_headers, normal_user, other_user
):
    """Two accounts may both know a "Dana Scully"; neither learns of the other's.

    The ``Speaker.user_id`` filter is skipped for admins by design, so a dropped
    filter here would silently publish other accounts' filenames to every user.
    """
    mine = make_media_file(db_session, int(normal_user.id))
    theirs = make_media_file(db_session, int(other_user.id))
    speaker = _make_speaker(
        db_session, normal_user, mine, name="SPEAKER_00", display_name="Dana Scully"
    )
    _make_speaker(db_session, other_user, theirs, name="SPEAKER_00", display_name="Dana Scully")

    response = client.get(f"{SPEAKERS}/{speaker.uuid}/cross-media", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert [entry["media_file_id"] for entry in response.json()] == [str(mine.uuid)]


def test_cross_media_on_a_quarantined_speakers_own_file_is_404(
    client, db_session, user_token_headers, normal_user
):
    """Adversarial-review follow-up (A2's leak class): the anchor speaker's own
    file is quarantined. ``PermissionService.get_file_permission`` has no
    notion of quarantine (ownership/sharing only) and would have returned
    ``"owner"`` here, so without an explicit ``is_hidden_for`` check the
    caller's own quarantined file's speaker data — filenames, titles, upload
    times of every occurrence — stayed fully reachable through this endpoint
    even though the file itself 404s everywhere else, for its own owner."""
    media_file = make_media_file(db_session, int(normal_user.id))
    media_file.is_quarantined = True
    db_session.commit()
    speaker = _make_speaker(
        db_session, normal_user, media_file, name="SPEAKER_00", display_name="Dana Scully"
    )

    response = client.get(f"{SPEAKERS}/{speaker.uuid}/cross-media", headers=user_token_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_cross_media_excludes_a_quarantined_same_named_occurrence(
    client, db_session, user_token_headers, normal_user
):
    """The anchor file is fine; a SECOND same-named-speaker file the caller owns
    gets quarantined afterward. That occurrence's filename/title/upload_time
    must drop out of the result — the endpoint must not surface a quarantined
    file's metadata just because it also holds a matching speaker name."""
    visible = make_media_file(db_session, int(normal_user.id))
    hidden = make_media_file(db_session, int(normal_user.id))
    hidden.is_quarantined = True
    db_session.commit()
    speaker = _make_speaker(
        db_session, normal_user, visible, name="SPEAKER_00", display_name="Dana Scully"
    )
    _make_speaker(db_session, normal_user, hidden, name="SPEAKER_03", display_name="Dana Scully")

    response = client.get(f"{SPEAKERS}/{speaker.uuid}/cross-media", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert [entry["media_file_id"] for entry in response.json()] == [str(visible.uuid)]


def test_cross_media_on_someone_elses_speaker_is_403(
    client, db_session, other_user_auth_headers, normal_user
):
    media_file = make_media_file(db_session, int(normal_user.id))
    speaker = _make_speaker(
        db_session, normal_user, media_file, name="SPEAKER_00", display_name="Dana Scully"
    )

    response = client.get(f"{SPEAKERS}/{speaker.uuid}/cross-media", headers=other_user_auth_headers)

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_cross_media_for_an_unknown_speaker_is_404(client, user_token_headers):
    response = client.get(f"{SPEAKERS}/{ABSENT_UUID}/cross-media", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_cross_media_requires_authentication(client):
    response = client.get(f"{SPEAKERS}/{ABSENT_UUID}/cross-media")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /tags/{tag_uuid}/files
# ---------------------------------------------------------------------------
def test_tag_files_lists_the_accessible_files_carrying_the_tag(
    client, db_session, user_token_headers, normal_user
):
    """``total`` is the real count and ``files`` carries pre-formatted display fields.

    The manager renders ``display_title`` directly — a null there is a blank row, and
    the fallback chain (title → filename → uuid) is the backend's job, not the SPA's.
    """
    tag = _make_tag(db_session, int(normal_user.id))
    tagged = make_media_file(db_session, int(normal_user.id))
    make_media_file(db_session, int(normal_user.id))  # untagged, must not appear
    _attach(db_session, tag, tagged)

    response = client.get(f"{TAGS}/{tag.uuid}/files", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert [f["uuid"] for f in body["files"]] == [str(tagged.uuid)]
    assert body["files"][0]["display_title"] == tagged.filename
    assert body["files"][0]["status"] == "completed"


def test_tag_files_reports_the_true_total_above_the_limit(
    client, db_session, user_token_headers, normal_user
):
    """``limit`` caps the page but never the count, so the UI can say "and N more".

    Catches ``total`` being computed from the truncated list — the manager would then
    claim a tag touches exactly ``limit`` files however many it really touches.
    """
    tag = _make_tag(db_session, int(normal_user.id))
    for _ in range(3):
        _attach(db_session, tag, make_media_file(db_session, int(normal_user.id)))

    response = client.get(
        f"{TAGS}/{tag.uuid}/files", headers=user_token_headers, params={"limit": 2}
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 3
    assert len(body["files"]) == 2


def test_tag_files_hides_another_users_files_carrying_a_shared_system_tag(
    client, db_session, user_token_headers, normal_user, other_user
):
    """A system tag is visible to everyone; its *files* are still access-scoped.

    This is the leak a naive "list every file with this tag" implementation would
    produce — the tag is readable by all, so only ``files_for_tag``'s accessible-file
    subquery stands between the caller and every account's filenames.
    """
    system_tag = _make_tag(db_session, None)
    mine = make_media_file(db_session, int(normal_user.id))
    theirs = make_media_file(db_session, int(other_user.id))
    _attach(db_session, system_tag, mine)
    _attach(db_session, system_tag, theirs)

    response = client.get(f"{TAGS}/{system_tag.uuid}/files", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [f["uuid"] for f in body["files"]] == [str(mine.uuid)]
    assert body["total"] == 1


def test_tag_files_on_an_invisible_tag_is_404(
    client, db_session, other_user_auth_headers, normal_user
):
    """A tag that reaches nobody answers 404, so probing cannot enumerate tags.

    404 rather than 403 is the tag plane's deliberate choice throughout — see
    ``backend/app/api/CLAUDE.md`` on the three tag scopes.
    """
    tag = _make_tag(db_session, int(normal_user.id))
    _attach(db_session, tag, make_media_file(db_session, int(normal_user.id)))

    response = client.get(f"{TAGS}/{tag.uuid}/files", headers=other_user_auth_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_tag_files_for_an_unknown_tag_is_404(client, user_token_headers):
    response = client.get(f"{TAGS}/{ABSENT_UUID}/files", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.parametrize("limit", [0, 201])
def test_tag_files_rejects_a_limit_outside_the_bounds(client, user_token_headers, limit):
    """``ge=1, le=200`` — the page size is the server's decision, not the caller's."""
    response = client.get(
        f"{TAGS}/{ABSENT_UUID}/files", headers=user_token_headers, params={"limit": limit}
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_tag_files_requires_authentication(client):
    response = client.get(f"{TAGS}/{ABSENT_UUID}/files")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
