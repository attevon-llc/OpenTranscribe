"""The date filter, against a real Postgres and real permission rules (#403 R7).

``test_chat_aggregation.py`` mocks everything and cannot reach this: the filter now
resolves ``media_file.recorded_date`` **relationally**, through the same
``get_accessible_file_ids_subquery`` the rest of the app uses, so proving it works means
proving it against rows and an access rule rather than against a dict that was handed back.

What each test here is guarding, and why a mocked version would not:

* **It filters on the recorded date, not the upload date.** Every row these tests create is
  uploaded *now* and recorded in 2025 — the exact shape of a back-catalogue import, and the
  exact shape of the eval corpus, where ``upload_time`` had one distinct value across all
  432 files. A filter on ``upload_time`` scores 0 here and cannot be made to pass.
* **Undated files are reported, not silently dropped.** Narrowing to rows that *have* a
  date excludes every row that does not. On a library the resolver has not swept that is
  most of them, so a bare count would be a new silent wrong answer replacing the old one.
* **Another user's recording in the same month is not counted.** The date filter must not
  become a way around the access rule.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg
from typing import Any

import pytest

from app.services.chat.aggregation_service import _files_in_period
from app.services.chat.aggregation_service import answer_aggregation
from app.services.chat.router import route

pytestmark = pytest.mark.unit

MARCH = ("2025-03-01", "2025-04-01")


class _RecordingClient:
    """Records every body it is handed and replays a canned response.

    ``sessions_live_at_search`` is what pins the session-lifetime rule: the date
    filter's Postgres statements must have finished and released their
    transaction before this search runs. See ``scripts/audit-session-lifetime.py``.
    """

    def __init__(self, response: dict[str, Any] | None = None, ledger: list[int] | None = None):
        self.response = response or {"aggregations": {}}
        self.bodies: list[dict[str, Any]] = []
        self.sessions_live_at_search: list[int] = []
        self._ledger = ledger

    def search(self, index: str, body: dict[str, Any], **kwargs) -> dict[str, Any]:  # noqa: ARG002
        self.bodies.append(body)
        if self._ledger is not None:
            self.sessions_live_at_search.append(self._ledger[0])
        return self.response


def _factory(db_session, ledger: list[int] | None = None):
    """A ``session_factory`` over the test's own savepoint-isolated session.

    The session cannot really be closed (the harness owns it), so ``ledger``
    counts entries and exits of the scope instead — which is the property under
    test: the scope must have been *left* before OpenSearch is called.
    """
    import contextlib

    @contextlib.contextmanager
    def _scope():
        if ledger is not None:
            ledger[0] += 1
        try:
            yield db_session
        finally:
            if ledger is not None:
                ledger[0] -= 1

    return _scope


@pytest.fixture
def owner(db_session):
    from app.models.user import User

    user = User(
        email=f"rd-filter-{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        auth_type="local",
    )
    db_session.add(user)
    db_session.flush()
    return user


def _add_file(db_session, user, *, recorded, source, filename="clip.wav"):
    """A file uploaded NOW and recorded whenever ``recorded`` says.

    That gap is the whole point: it is what distinguishes a filter on the recorded date
    from a filter on the upload date, and no test here can pass under the latter.
    """
    from app.models.media import MediaFile

    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=filename,
        storage_path=f"x/{uuid_pkg.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        upload_time=dt.datetime.now(dt.UTC),
        recorded_date=recorded,
        recorded_date_source=source,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def test_it_selects_by_the_recorded_date_and_not_the_upload_date(db_session, owner):
    inside = _add_file(
        db_session, owner, recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC), source="filename"
    )
    _add_file(db_session, owner, recorded=dt.datetime(2025, 4, 2, tzinfo=dt.UTC), source="filename")

    found = _files_in_period(db_session, MARCH, owner.id, None, None)
    assert found is not None
    uuids, sources, undated = found
    assert uuids == [str(inside.uuid)]
    assert sources == {"filename": 1}
    assert undated == 0


def test_the_period_is_half_open_so_a_month_boundary_lands_in_exactly_one_month(db_session, owner):
    """1 March is in; 1 April is not. An inclusive upper bound double-counts every
    boundary recording across two adjacent monthly questions."""
    first = _add_file(
        db_session, owner, recorded=dt.datetime(2025, 3, 1, tzinfo=dt.UTC), source="container"
    )
    _add_file(
        db_session, owner, recorded=dt.datetime(2025, 4, 1, tzinfo=dt.UTC), source="container"
    )

    found = _files_in_period(db_session, MARCH, owner.id, None, None)
    assert found is not None
    assert found[0] == [str(first.uuid)]


def test_undated_files_are_counted_and_reported_rather_than_silently_dropped(db_session, owner):
    """The honesty property that replaces the old ``upload_time`` disclosure.

    "3 in March" over a library where 40 recordings have no known date is a floor, not
    an answer, and the difference has to reach the user.
    """
    _add_file(
        db_session, owner, recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC), source="transcript"
    )
    _add_file(db_session, owner, recorded=None, source=None)
    _add_file(db_session, owner, recorded=None, source="none")

    found = _files_in_period(db_session, MARCH, owner.id, None, None)
    assert found is not None
    uuids, sources, undated = found
    assert len(uuids) == 1
    assert sources == {"transcript": 1}
    assert undated == 2, "both unresolved rows must be reported, including source='none'"


def test_the_source_tally_distinguishes_where_each_date_came_from(db_session, owner):
    """ "3 meetings in March — dates from filenames" is checkable; a bare 3 is not."""
    for source in ("container", "filename", "filename", "manual"):
        _add_file(db_session, owner, recorded=dt.datetime(2025, 3, 5, tzinfo=dt.UTC), source=source)

    found = _files_in_period(db_session, MARCH, owner.id, None, None)
    assert found is not None
    assert found[1] == {"container": 1, "filename": 2, "manual": 1}


def test_another_users_recording_in_the_same_month_is_not_reachable(db_session, owner):
    """The date filter must not become a way around the access rule."""
    from app.models.user import User

    stranger = User(
        email=f"rd-stranger-{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        auth_type="local",
    )
    db_session.add(stranger)
    db_session.flush()

    mine = _add_file(
        db_session, owner, recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC), source="filename"
    )
    _add_file(
        db_session, stranger, recorded=dt.datetime(2025, 3, 11, tzinfo=dt.UTC), source="filename"
    )

    found = _files_in_period(db_session, MARCH, owner.id, None, None)
    assert found is not None
    assert found[0] == [str(mine.uuid)]


def test_an_explicit_scope_is_narrowed_not_replaced(db_session, owner):
    """A chat scoped to two recordings must not widen to the whole month."""
    scoped = _add_file(
        db_session, owner, recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC), source="filename"
    )
    unscoped = _add_file(
        db_session, owner, recorded=dt.datetime(2025, 3, 11, tzinfo=dt.UTC), source="filename"
    )

    found = _files_in_period(db_session, MARCH, owner.id, None, [str(scoped.uuid)])
    assert found is not None
    assert found[0] == [str(scoped.uuid)]
    assert str(unscoped.uuid) not in found[0]


def test_an_empty_scope_still_means_match_nothing(db_session, owner):
    """``file_uuids=[]`` is "match nothing" everywhere in this package; inverting it here
    would leak the whole library through a date question."""
    _add_file(
        db_session, owner, recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC), source="filename"
    )
    found = _files_in_period(db_session, MARCH, owner.id, None, [])
    assert found is not None
    assert found[0] == []


def test_too_many_matches_decline_rather_than_returning_a_truncated_list(
    db_session, owner, monkeypatch
):
    """A truncated bucket list is a wrong answer that looks like a right one.

    The cap is patched down rather than creating 501 rows: the behaviour under test is
    "what happens past the cap", not the cap's value, and a 501-row fixture would make
    this the slowest test in the suite for no extra assurance.
    """
    import app.core.constants as constants

    monkeypatch.setattr(constants, "CHAT_MAX_SCOPE_FILES", 2)
    for _ in range(3):
        _add_file(
            db_session, owner, recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC), source="filename"
        )

    assert _files_in_period(db_session, MARCH, owner.id, None, None) is None


def test_at_the_cap_it_still_answers(db_session, owner, monkeypatch):
    """The control for the decline above — an off-by-one here silently loses answers."""
    import app.core.constants as constants

    monkeypatch.setattr(constants, "CHAT_MAX_SCOPE_FILES", 2)
    for _ in range(2):
        _add_file(
            db_session, owner, recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC), source="filename"
        )

    found = _files_in_period(db_session, MARCH, owner.id, None, None)
    assert found is not None
    assert len(found[0]) == 2


def test_the_whole_shape_reports_which_source_dated_the_files_it_counted(db_session, owner):
    """End to end: a "how many meetings in March 2025" turn, through the real shape.

    The assertion that matters is on ``coverage`` — it is what reaches the prompt, and
    base rule 10 makes the model report a counted block exactly. A number arriving with
    no statement of where its dates came from is the "silent lie at corpus scale" this
    change exists to prevent.
    """
    dated = _add_file(
        db_session, owner, recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC), source="filename"
    )
    _add_file(db_session, owner, recorded=None, source=None)

    question = "How many meetings in March 2025 discussed the Atlas migration?"
    client = _RecordingClient(
        {
            "aggregations": {
                "files": {
                    "sum_other_doc_count": 0,
                    "buckets": [
                        {
                            "key": str(dated.uuid),
                            "title": {"buckets": [{"key": "March standup"}]},
                        }
                    ],
                }
            }
        }
    )
    result = answer_aggregation(
        question,
        route(question),
        session_factory=_factory(db_session),
        client=client,
        index="i",
        user_id=owner.id,
    )
    assert result is not None
    assert result.count == 1
    assert result.coverage["date_filter"] == "recorded_date"
    assert result.coverage["date_filter_period"] == "2025-03-01..2025-04-01"
    assert result.coverage["date_sources"] == {"filename": 1}
    assert result.coverage["undated_files_excluded"] == 1

    # And the narrowed scope reached OpenSearch as a uuid restriction rather than as a
    # date range — the index is never asked about the recorded date.
    body = str(client.bodies[0])
    assert str(dated.uuid) in body
    assert "recorded_date" not in body
    assert "2025-03-01" not in body


def test_the_date_filters_transaction_is_released_before_opensearch_is_called(db_session, owner):
    """The Postgres phase must not still be open when the aggregation search runs.

    This is the shape ``scripts/audit-session-lifetime.py`` gates: ``_files_in_period``
    runs two statements, and if its transaction were still open across the ``size: 0``
    search the turn would sit ``idle in transaction`` for an OpenSearch round trip —
    holding ``ACCESS SHARE`` and queueing every ``ALTER TABLE`` behind a chat message.
    """
    _add_file(
        db_session, owner, recorded=dt.datetime(2025, 3, 10, tzinfo=dt.UTC), source="filename"
    )

    ledger = [0]
    question = "How many meetings in March 2025 discussed the Atlas migration?"
    client = _RecordingClient(
        {"aggregations": {"files": {"sum_other_doc_count": 0, "buckets": []}}}, ledger=ledger
    )

    answer_aggregation(
        question,
        route(question),
        session_factory=_factory(db_session, ledger),
        client=client,
        index="i",
        user_id=owner.id,
    )

    assert client.sessions_live_at_search == [0], (
        "the date filter's session was still open during the OpenSearch aggregation"
    )
    assert ledger[0] == 0, "a session scope was left open after the aggregation returned"
