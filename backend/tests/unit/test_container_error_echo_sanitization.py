"""Scanner 4's remediation at the service layer (#914 container-taint).

The service-layer half of ``tests/api/test_container_error_echo_sanitization.py``.
Each of these four functions writes a caught exception into a container it then
returns, and each of those containers was traced to a real response body:

===========================================  ==================================================
function                                     traced consumer
===========================================  ==================================================
``gdpr_erasure_service.erase_user``          ``admin.py:2527`` ``return erase_user(...)``
``gdpr_erasure_service.erase_organization``  ``org_admin.py:186`` ``return summary``
``media_mirror_engine.execute_mirror``       ``record_result`` -> ``backup.mirror_last_result``
                                             -> ``media_mirror_service.get_settings()``'s
                                             ``last_result`` -> the mirror settings response
``llm_service._process_multiple_chunks``     ``_combine_sections`` json.dumps's it into the
                                             COMBINER PROMPT sent to the LLM provider
===========================================  ==================================================

Same three assertions as the API file: the outcome, the SENTINEL's ABSENCE from the
returned/egressed value, and its PRESENCE in ``caplog``. Plus, in each, that the safe
half survived — the subject id, the object key, the section number.
"""

from __future__ import annotations

import logging
from typing import Any
from typing import cast
from unittest import mock

from sqlalchemy.orm import Session

from app.models.erasure import ErasureLedgerEntry
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.user import User
from app.services import gdpr_erasure_service as gdpr
from app.services import media_mirror_engine as eng
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService
from app.services.media_mirror_engine import SourceObject

SENTINEL = "SENTINEL-svc-/srv/internal/secret-path-and-redis://:hunter2@broker:6379/0"


# =============================================================================
# media_mirror_engine.execute_mirror
# =============================================================================
class _ExplodingDestination:
    """Implements only the ``MirrorDestination`` protocol; every copy fails."""

    def lookup(self, key: str) -> tuple[int, str | None] | None:
        return None

    def copy(self, obj: SourceObject) -> None:
        raise OSError(f"S3 PutObject refused: {SENTINEL}")


def test_execute_mirror_error_sample_does_not_echo_the_exception(caplog):
    with caplog.at_level(logging.ERROR):
        counters = eng.execute_mirror(
            [SourceObject("media/2026/recording.wav", 42, "etag")], _ExplodingDestination()
        )

    assert counters["objects_failed"] == 1
    joined = " ".join(counters["errors"])
    assert SENTINEL not in joined
    assert "hunter2" not in joined

    # The object KEY is what tells an admin which object to re-mirror — the whole
    # value of a sampled error list. It must survive the sanitization.
    assert counters["errors"] == ["media/2026/recording.wav: OSError"]

    assert SENTINEL in caplog.text


# =============================================================================
# llm_service._process_multiple_chunks
# =============================================================================
def test_section_failure_placeholder_does_not_echo_the_provider_exception(caplog):
    """The placeholder is json.dumps'd into the COMBINER PROMPT, so it egresses."""
    service = LLMService(LLMConfig(provider=LLMProvider.CUSTOM, model="m", base_url="http://x/v1"))

    def _raise(*args, **kwargs):
        raise RuntimeError(f"POST https://api.example.invalid/v1 failed: {SENTINEL}")

    combined_with: dict[str, list[dict[str, Any]]] = {}

    def _capture_sections(sections, *args, **kwargs):
        combined_with["sections"] = sections
        return {"ok": True}

    with (
        mock.patch.object(service, "_summarize_section", _raise),
        mock.patch.object(service, "_combine_sections", _capture_sections),
        caplog.at_level(logging.ERROR),
    ):
        service._process_multiple_chunks(["chunk one", "chunk two"], None, "{transcript}")

    sections = combined_with["sections"]
    assert SENTINEL not in str(sections)
    assert "hunter2" not in str(sections)

    # The reader still learns WHICH section failed and that it failed.
    assert sections[0]["key_points"] == ["Section 1: Processing failed (RuntimeError)"]
    assert sections[1]["key_points"] == ["Section 2: Processing failed (RuntimeError)"]

    assert SENTINEL in caplog.text


# =============================================================================
# gdpr_erasure_service — a fake session, so no dev data is touched
# =============================================================================
class _FakeQuery:
    def __init__(self, first_value: object | None) -> None:
        self._first = first_value

    def filter(self, *args, **kwargs) -> _FakeQuery:
        return self

    def all(self) -> list:
        return []

    def first(self) -> object | None:
        return self._first

    def count(self) -> int:
        return 0

    def delete(self, *args, **kwargs) -> int:
        return 0

    def update(self, *args, **kwargs) -> int:
        return 0


class _FakeSession:
    """Minimal Session stand-in. ``delete`` raises; everything else is inert.

    Deliberately NOT the ``db_session`` fixture: ``erase_user``/``erase_organization``
    destroy object-storage and OpenSearch documents, which the savepoint harness
    cannot roll back, and this test is about one ``except`` branch — not about the
    erasure itself.
    """

    def __init__(self, present: dict[type, object], delete_error: Exception) -> None:
        self._present = present
        self._delete_error = delete_error
        self.rolled_back = False

    def query(self, model, *rest) -> _FakeQuery:
        return _FakeQuery(self._present.get(model))

    def delete(self, obj) -> None:
        raise self._delete_error

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        self.rolled_back = True

    def add(self, obj) -> None:
        pass


#: Real ORM instances, never attached to a session — so mypy checks these exactly as it
#: checks the production call sites, rather than being told to trust a stand-in.
_LEDGER_ENTRY = ErasureLedgerEntry(id=7, uuid="00000000-0000-0000-0000-0000000000ff")


def _integrity_error() -> Exception:
    """The real shape: SQLAlchemy quotes the statement and its parameters."""
    return RuntimeError(
        f'UPDATE "user" SET ... WHERE id = %(id_1)s -- referenced from media_file at {SENTINEL}'
    )


def test_erase_user_row_delete_failure_does_not_echo_the_sql(caplog):
    subject = User(
        id=4242, email="subject@example.invalid", uuid="11111111-1111-1111-1111-111111111111"
    )
    db = _FakeSession(
        {User: subject, MediaFile: None, ErasureLedgerEntry: _LEDGER_ENTRY},
        _integrity_error(),
    )

    # Only the three seams that would reach OpenSearch / the audit store / the
    # ledger's own writes are patched. `_purge_files` and `_delete_owner_scoped_rows`
    # run FOR REAL against the fake session (which yields no rows), and
    # `ledger_entry=` is passed rather than patching `record_request`.
    with (
        mock.patch.object(gdpr, "_erase_speaker_voiceprints", return_value=0),
        mock.patch.object(gdpr.ledger, "record_outcome"),
        mock.patch.object(gdpr.audit_logger, "log"),
        caplog.at_level(logging.ERROR),
    ):
        summary = gdpr.erase_user(
            # The ONE cast in this file: `_FakeSession` implements the handful of
            # Session methods these two functions call and nothing else, which is the
            # point (see its docstring) — there is no narrower way to tell mypy that.
            cast(Session, db),
            4242,
            actor_user_id=1,
            actor_email="admin@example.invalid",
            ledger_entry=_LEDGER_ENTRY,
        )

    assert db.rolled_back is True
    assert summary["users_deleted"] == 0
    assert SENTINEL not in str(summary)
    assert 'UPDATE "user"' not in str(summary)

    # The subject id — the only thing that makes a partial erasure actionable —
    # survives, and so does the fact that the row delete is what failed.
    assert summary["errors"] == [
        {"user_id": 4242, "error": "user row delete failed (RuntimeError)"}
    ]

    assert SENTINEL in caplog.text


def test_erase_organization_row_delete_failure_does_not_echo_the_sql(caplog):
    org = Organization(id=99, name="Subject Org", uuid="22222222-2222-2222-2222-222222222222")
    db = _FakeSession(
        {Organization: org, ErasureLedgerEntry: _LEDGER_ENTRY},
        _integrity_error(),
    )

    with (
        mock.patch.object(gdpr, "_erase_speaker_voiceprints", return_value=0),
        mock.patch.object(gdpr.ledger, "record_outcome"),
        mock.patch.object(gdpr.audit_logger, "log"),
        caplog.at_level(logging.ERROR),
    ):
        summary = gdpr.erase_organization(
            cast(Session, db),
            99,
            actor_user_id=1,
            actor_email="a@example.invalid",
            ledger_entry=_LEDGER_ENTRY,
        )

    assert db.rolled_back is True
    assert SENTINEL not in str(summary)
    assert 'UPDATE "user"' not in str(summary)
    assert summary["errors"] == [
        {"org_id": 99, "error": "organization row delete failed (RuntimeError)"}
    ]

    assert SENTINEL in caplog.text
