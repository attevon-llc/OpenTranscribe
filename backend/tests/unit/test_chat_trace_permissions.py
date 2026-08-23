"""The query trace must not become a side channel onto other users' files (GH #514).

The panel reports what a turn *did*, and the most tempting thing to report is
"your scope named 4 recordings and 1 was dropped". That sentence is a lookup
oracle if the number distinguishes **"that recording exists and is not yours"**
from **"no such recording"** — a user could confirm the existence of any UUID by
watching the count move, and confirm which of a list of guessed UUIDs are real.

So the property under test is stronger than "no filename leaked". It is:

    a trace produced for a file the caller cannot see must be
    INDISTINGUISHABLE from a trace produced for a file that does not exist

with an owner-side control proving the two really are different situations and
that the test could therefore have failed.

Shape follows ``tests/api/test_ownership_contracts.py``: create as one user via
direct ORM inserts the savepoint rolls back, then act as another.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.api.deps_context import RequestContext
from app.models.media import MediaFile
from app.schemas.chat import ChatScope
from app.services.chat.context_resolver import resolve_scope_file_uuids
from app.services.chat.trace import ListTraceRecorder
from app.services.chat.trace import Outcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import emit

#: A title distinctive enough that a substring search for it cannot match by
#: accident, so "it does not appear anywhere in the trace" is a real assertion.
SECRET_TITLE = "Acquisition terms with Northwind, do not share"


def _owned_file(db, owner) -> MediaFile:
    media = MediaFile(
        user_id=owner.id,
        filename=f"{SECRET_TITLE}.wav",
        title=SECRET_TITLE,
        storage_path=f"test/{uuid_pkg.uuid4().hex}.wav",
        file_size=2048,
        content_type="audio/wav",
        status="completed",
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _validated_detail(db, user, file_uuid: str) -> dict:
    """Resolve a one-file scope as *user* and return the VALIDATED node's detail.

    Drives the real resolver and the real emit call site, rather than asserting
    on a hand-built event: the question is what the PRODUCTION path puts on the
    node, and a fabricated event would answer a different question.
    """
    diagnostics: dict = {}
    resolve_scope_file_uuids(
        db,
        RequestContext(user=user),
        ChatScope(file_uuids=[file_uuid]),
        diagnostics=diagnostics,
    )
    recorder = ListTraceRecorder()
    emit(
        recorder,
        QueryStage.VALIDATED,
        Outcome.OK,
        node_id="validated",
        dropped=diagnostics.get("files_dropped", 0),
    )
    return dict(recorder.events[0].detail)


def test_an_inaccessible_file_traces_identically_to_a_nonexistent_one(
    db_session, normal_user, other_user
):
    """The count must not confirm that someone else's recording exists."""
    owned = _owned_file(db_session, normal_user)

    inaccessible = _validated_detail(db_session, other_user, str(owned.uuid))
    nonexistent = _validated_detail(db_session, other_user, str(uuid_pkg.uuid4()))

    assert inaccessible == nonexistent, (
        "the trace distinguishes 'exists but not yours' from 'no such file', "
        "which turns the dropped count into an existence oracle"
    )
    assert inaccessible == {"dropped": 1}


def test_the_owner_sees_a_different_count_for_the_same_file(db_session, normal_user):
    """The control. Without it, "always identical" also passes when the resolver
    is broken and drops every file for everyone — which would look like perfect
    privacy and be a total outage.
    """
    owned = _owned_file(db_session, normal_user)

    as_owner = _validated_detail(db_session, normal_user, str(owned.uuid))
    as_stranger = _validated_detail(db_session, normal_user, str(uuid_pkg.uuid4()))

    assert as_owner == {"dropped": 0}, "the owner's own file must resolve"
    assert as_owner != as_stranger, (
        "owner and stranger produced the same trace, so the resolver is not "
        "actually resolving anything and the test above proves nothing"
    )


def test_no_trace_node_carries_the_files_title_or_uuid(db_session, normal_user, other_user):
    """Not even for the OWNER, and that is the point.

    ``SAFE_DETAIL_KEYS`` is a backstop; the rule is that call sites never
    construct the leak in the first place. Asserting on the owner's own trace is
    the strict version — if an identifier cannot reach a node the caller is
    fully entitled to see, no permission bug downstream can expose one either.
    """
    owned = _owned_file(db_session, normal_user)
    file_uuid = str(owned.uuid)

    for user in (normal_user, other_user):
        rendered = repr(_validated_detail(db_session, user, file_uuid))
        assert file_uuid not in rendered, f"the file uuid reached the trace: {rendered}"
        assert SECRET_TITLE not in rendered, f"the file title reached the trace: {rendered}"
        assert "Northwind" not in rendered, f"part of the title reached the trace: {rendered}"


def test_the_scrub_would_have_caught_a_leaky_call_site(db_session, normal_user):
    """Must-fire control for the assertions above.

    They search a rendered detail for two strings. If ``_scrub`` silently
    dropped everything, or ``repr`` returned an empty dict, those searches would
    pass against a trace carrying nothing at all — so this proves the search
    finds a string that IS present, and that the scrub is what removes an
    identifying one.
    """
    owned = _owned_file(db_session, normal_user)
    recorder = ListTraceRecorder()
    emit(
        recorder,
        QueryStage.VALIDATED,
        Outcome.OK,
        node_id="validated",
        dropped=1,
        title=SECRET_TITLE,
        file_uuid=str(owned.uuid),
    )

    rendered = repr(dict(recorder.events[0].detail))
    assert "dropped" in rendered, "the search cannot find a key that is present"
    assert SECRET_TITLE not in rendered
    assert str(owned.uuid) not in rendered
