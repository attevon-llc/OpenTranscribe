"""The user-editable recorded date, over the real HTTP surface (#403 R7, requirement c).

The owner's framing: *"If we need UI components for users to add and edit metadata, then
that is required, otherwise it would be false data reported."* A derived date the user
cannot see the origin of, or correct, is worse than no date — so the API contract has three
parts and all three are pinned here:

**a.** the date and its source travel together, always, in both directions;
**b.** the source is on the wire so the UI can show it — including in the gallery list,
where a wrong date does the most damage because it is scanned rather than read;
**c.** the user's value outranks every derived source **permanently**.

The last one is the one worth a test rather than a comment: an edit that a later reindex
silently reverts is not an edit, and the whole point of ``recorded_date_locked`` is that
the correction survives re-derivation. ``test_a_re_derivation_does_not_overwrite_the_users_
value`` runs the real resolver against a filename whose date is *different* from the one the
user set, so a lock that did not hold would produce a visibly different date rather than an
identical one.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile

pytestmark = pytest.mark.unit


def _make_file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        # A filename that ENCODES A DIFFERENT DATE than any test sets by hand, so the
        # automatic source and the manual one can never be confused for one another.
        "filename": "2019-06-02_archive-standup.wav",
        "title": "archive standup",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def test_a_user_can_set_the_recorded_date_and_it_is_marked_manual(
    client, user_token_headers, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)

    response = client.put(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        json={"recorded_date": "2024-03-15T00:00:00Z"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["recorded_date"].startswith("2024-03-15")

    provenance = body["recorded_date_provenance"]
    assert provenance is not None, "a date must never reach a client without its origin"
    assert provenance["source"] == "manual"
    assert provenance["locked"] is True
    assert provenance["confidence"] == 1.0


def test_a_re_derivation_does_not_overwrite_the_users_value(
    client, user_token_headers, normal_user, db_session
):
    """The guarantee that makes the edit real rather than nominal.

    The file's name says 2019-06-02 and the user says 2024-03-15. Running the real
    resolver afterwards must leave 2024-03-15 standing. A lock that did not hold would
    show up as the *filename's* date, which is a different value — not merely a missing
    flag, so this cannot pass by accident.
    """
    from app.services.ingest_artifacts.recorded_date_service import resolve_for_file

    media_file = _make_file(db_session, normal_user)
    client.put(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        json={"recorded_date": "2024-03-15T00:00:00Z"},
    )

    resolve_for_file(db_session, media_file.id)
    db_session.commit()
    db_session.refresh(media_file)

    assert media_file.recorded_date is not None, "a manual edit must persist a date"
    assert media_file.recorded_date.date() == dt.date(2024, 3, 15)
    assert media_file.recorded_date_source == "manual"
    assert media_file.recorded_date_locked is True


def test_the_filename_date_is_what_the_resolver_would_have_chosen(
    client, user_token_headers, normal_user, db_session
):
    """The control for the test above.

    Without it, a resolver that found nothing at all would make the lock test pass while
    proving nothing about locking. This asserts the same file, unlocked, resolves to the
    filename's 2019-06-02 — so the previous test's 2024-03-15 is a value the lock
    actively defended, not the only value available.
    """
    from app.services.ingest_artifacts.recorded_date_service import resolve_for_file

    media_file = _make_file(db_session, normal_user)

    resolve_for_file(db_session, media_file.id)
    db_session.commit()
    db_session.refresh(media_file)

    assert media_file.recorded_date is not None, "resolution must persist a date"
    assert media_file.recorded_date.date() == dt.date(2019, 6, 2)
    assert media_file.recorded_date_source == "filename"
    assert media_file.recorded_date_locked is False


def test_clearing_the_date_returns_the_file_to_automatic_resolution(
    client, user_token_headers, normal_user, db_session
):
    """A mistaken correction must be retractable.

    Leaving the row locked at NULL would disable the resolver for that file forever —
    the user would have no way back, and no indication that was what happened.
    """
    from app.services.ingest_artifacts.recorded_date_service import resolve_for_file

    media_file = _make_file(db_session, normal_user)
    client.put(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        json={"recorded_date": "2024-03-15T00:00:00Z"},
    )

    response = client.put(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        json={"recorded_date": None},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["recorded_date"] is None
    assert response.json()["recorded_date_provenance"] is None

    db_session.refresh(media_file)
    assert media_file.recorded_date_locked is False
    resolve_for_file(db_session, media_file.id)
    db_session.commit()
    db_session.refresh(media_file)
    assert media_file.recorded_date is not None, (
        "clearing must re-enable resolution, so a date must be present again"
    )
    assert media_file.recorded_date.date() == dt.date(2019, 6, 2), (
        "clearing must re-enable automatic resolution, not merely blank the value"
    )


def test_an_unrelated_edit_does_not_disturb_the_recorded_date(
    client, user_token_headers, normal_user, db_session
):
    """``exclude_unset`` is what separates "not mentioned" from "set to null".

    Get that wrong and every rename silently wipes the user's date — a data-loss bug
    with no error and no trace, on the field this whole change exists to make
    trustworthy.
    """
    media_file = _make_file(db_session, normal_user)
    client.put(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        json={"recorded_date": "2024-03-15T00:00:00Z"},
    )

    response = client.put(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        json={"title": "renamed, nothing to do with dates"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] == "renamed, nothing to do with dates"
    assert response.json()["recorded_date"].startswith("2024-03-15")
    assert response.json()["recorded_date_provenance"]["locked"] is True


def test_the_gallery_list_carries_the_provenance_too(
    client, user_token_headers, normal_user, db_session
):
    """The list is the surface a user SCANS, so it is where an unattributed date is worst.

    The detail page and the list are built by two different functions
    (``crud.get_media_file_detail`` and ``FormattingService.format_media_file_response``);
    wiring one and not the other is the realistic mistake, and it would leave every
    gallery date unattributed while the detail page looked correct.
    """
    media_file = _make_file(db_session, normal_user)
    client.put(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        json={"recorded_date": "2024-03-15T00:00:00Z"},
    )

    response = client.get("/api/files", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    listed = [row for row in response.json()["items"] if row["uuid"] == str(media_file.uuid)]
    assert listed, "the file under test is not in the list response"
    assert listed[0]["recorded_date"].startswith("2024-03-15")
    assert listed[0]["recorded_date_provenance"]["source"] == "manual"
    assert listed[0]["recorded_date_provenance"]["locked"] is True


def test_an_unresolved_file_reports_no_provenance_rather_than_a_fabricated_one(
    client, user_token_headers, normal_user, db_session
):
    """NULL provenance means "not yet resolved" and must not be dressed up as an answer.

    Distinct from ``source='none'``, which means every source was consulted and none
    answered. An un-swept library and a library of undatable recordings look identical
    if these collapse, and only one of them is fixable.
    """
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["recorded_date"] is None
    assert response.json()["recorded_date_provenance"] is None
