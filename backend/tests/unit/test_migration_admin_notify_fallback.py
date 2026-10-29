"""The ``user_id or 1`` fallback misdirected admin-migration events (issue #908, finding B).

``embedding_migration_v4.py``, ``speaker_attribute_migration_task.py`` and
``combined_speaker_analysis_task.py`` each had 2-3 ``send_ws_event(user_id or 1, ...)``
call sites. Account id 1 need not be an admin — a user-management cleanup or a
fresh install can leave it unoccupied, reassigned, or held by an ordinary user —
so a deployment-wide migration dispatched with no requesting admin (``user_id is
None``) silently misdirected its progress/completion events, including a
``failed_files`` list of file UUIDs, to whichever account happened to hold that
id.

``opensearch_integrity_task.py::_notify`` already had the correct fix (gate on
``user_id is None``, log instead of guessing a recipient); each of the three
modules above now share ``migration_admin_notify.notify_migration_admin``, which copies
that pattern. This file pins it: ``None`` logs and sends nothing, a real id is used
VERBATIM (never coerced to ``1``), and — the specific regression this closes —
id ``1`` is not special-cased into skipping the send.
"""

from __future__ import annotations

from app.services import migration_admin_notify


def test_none_user_id_logs_and_sends_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        migration_admin_notify, "send_ws_event", lambda *a, **kw: calls.append((a, kw))
    )

    migration_admin_notify.notify_migration_admin(None, "some.event", {"failed_files": ["uuid-1"]})

    assert calls == []


def test_a_real_user_id_is_used_verbatim_never_coerced_to_one(monkeypatch):
    calls = []
    monkeypatch.setattr(migration_admin_notify, "send_ws_event", lambda *a, **kw: calls.append(a))

    migration_admin_notify.notify_migration_admin(42, "some.event", {"total_files": 3})

    assert len(calls) == 1
    user_id, event_type, payload = calls[0]
    assert user_id == 42, "must be the real requesting admin, never the id-1 fallback"
    assert event_type == "some.event"
    assert payload == {"total_files": 3}


def test_account_id_one_is_not_special_cased_into_skipping(monkeypatch):
    """The regression this closes: `user_id or 1` treated a genuine id of 1 no
    differently than None — but id 1 is a normal (if coincidental) requester
    and must receive its own event like any other id."""
    calls = []
    monkeypatch.setattr(migration_admin_notify, "send_ws_event", lambda *a, **kw: calls.append(a))

    migration_admin_notify.notify_migration_admin(1, "some.event", {})

    assert len(calls) == 1
    assert calls[0][0] == 1
