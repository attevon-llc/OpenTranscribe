"""The library's languages must be offerable as a filter (#453).

Transcription has always been multilingual — WhisperX detects 100+ languages and
``MediaFile.language`` records the code on every file — but **nothing ever offered it
as a filter**, so a user with a mixed-language library had no way to narrow to one.
The chunk index has carried ``language`` as a filterable ``keyword`` the whole time,
and ``/search`` has accepted a ``language`` parameter; the gap was that the UI had no
list of languages to offer and never sent the parameter.

⚠️ **The plan for this work claimed the facet already existed and only needed
rendering. It did not.** ``language_kw`` in ``hybrid_search_service`` is a *per-file*
aggregation that populates each hit's metadata — it never produced a corpus-wide list
of distinct languages. Discovering that is why this endpoint change exists at all.

The list must come from the user's OWN library rather than a static table of every
language WhisperX supports: offering 100+ options where 99 match nothing is a filter
that wastes the user's time on every use.
"""

from __future__ import annotations

import uuid

from app.models.media import MediaFile


def _make_file(db, user, *, language: str | None) -> MediaFile:
    media = MediaFile(
        uuid=str(uuid.uuid4()),
        user_id=user.id,
        filename=f"{uuid.uuid4().hex[:8]}.mp4",
        storage_path=f"test/{uuid.uuid4().hex[:8]}.mp4",
        content_type="video/mp4",
        file_size=1000,
        language=language,
        status="completed",
    )
    db.add(media)
    db.flush()
    return media


def test_distinct_languages_are_offered(db_session, normal_user) -> None:
    """The defect: no endpoint exposed the languages, so no filter could be built."""
    from app.api.endpoints.files.filtering import get_metadata_filters

    for language in ("en", "es", "en", "ja"):
        _make_file(db_session, normal_user, language=language)
    db_session.flush()

    options = get_metadata_filters(db_session, normal_user.id, ownership="mine")

    assert options["languages"] == ["en", "es", "ja"], (
        "expected each language once, sorted — duplicates would render duplicate filter "
        f"buttons and an unstable order would move them between requests: {options}"
    )


def test_files_without_a_language_do_not_produce_an_empty_option(db_session, normal_user) -> None:
    """A blank chip filters to nothing and looks like a bug.

    ``language`` is nullable — a file that failed before detection, or was imported
    without transcription, has none.
    """
    from app.api.endpoints.files.filtering import get_metadata_filters

    _make_file(db_session, normal_user, language="en")
    _make_file(db_session, normal_user, language=None)
    _make_file(db_session, normal_user, language="")
    db_session.flush()

    options = get_metadata_filters(db_session, normal_user.id, ownership="mine")

    assert options["languages"] == ["en"]
    assert "" not in options["languages"] and None not in options["languages"]


def test_another_users_languages_are_not_offered(db_session, normal_user, admin_user) -> None:
    """The list is a filter over YOUR library; leaking another user's is a disclosure.

    It is small, but it is still information about a library the user cannot see —
    and the same query feeds `ownership="all"`, so the scoping has to be right here.
    """
    from app.api.endpoints.files.filtering import get_metadata_filters

    _make_file(db_session, normal_user, language="en")
    _make_file(db_session, admin_user, language="ko")
    db_session.flush()

    options = get_metadata_filters(db_session, normal_user.id, ownership="mine")

    assert "ko" not in options["languages"], (
        f"another user's language leaked into the filter list: {options['languages']}"
    )


def test_the_existing_options_still_come_back(db_session, normal_user) -> None:
    """The control: languages ride the SAME query as formats and codecs.

    Adding a third `array_agg` to that query is where they could have been dropped,
    and nothing else asserts the shape of this response.
    """
    from app.api.endpoints.files.filtering import get_metadata_filters

    _make_file(db_session, normal_user, language="en")
    db_session.flush()

    options = get_metadata_filters(db_session, normal_user.id, ownership="mine")

    for key in ("formats", "codecs", "languages", "duration", "file_size", "resolution"):
        assert key in options, f"{key} disappeared from the metadata filters: {sorted(options)}"
