"""A background label must not hold the speaker's row lock across its aggregate rebuild.

``process_speaker_update_background`` assigns the labeled speaker to a profile, then rebuilds
the profiles and clusters the move touched. It used to FLUSH the assignment (taking the row
lock on ``speaker``), run the privacy and impact reads, and only commit somewhere inside the
rebuild. A user merging that speaker away waits on the lock in ``DELETE FROM speaker``
(LOADRUN3: 329 ms max, ``Lock:transactionid``). The documented remedy is a short transaction
(PostgreSQL docs, "Explicit Locking"): every read first, then the writes, then the commit,
and only then the rebuild, which ``reconcile_speaker_impact`` requires anyway ("runs after
the caller's change has COMMITTED"). Ordered with a recording session; no database needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.api.endpoints import speakers_profile_workflow as workflow


class _RecordingSession:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self._added: list[Any] = []

    def add(self, obj: Any) -> None:
        self._added.append(obj)

    def flush(self) -> None:
        self.log.append("flush")
        for obj in self._added:
            if getattr(obj, "id", None) is None:
                obj.id = 900

    def commit(self) -> None:
        self.log.append("commit")


@pytest.fixture
def run(monkeypatch):
    """Label one speaker; returns the ordered log of reads, writes, commits and the rebuild."""
    from app.services import opensearch_service
    from app.services import speaker_cleanup
    from app.services import speaker_identity_rules
    from app.services import speaker_lookups

    log: list[str] = []

    def _read(name: str, value: Any):
        def _f(*_a, **_kw):
            log.append(f"read:{name}")
            return value

        return _f

    def _write(name: str):
        return lambda *_a, **_kw: log.append(f"write:{name}")

    class _Privacy:
        def __init__(self, _db) -> None:
            pass

        def voiceprints(self, _file_id):
            log.append("read:privacy")
            return SimpleNamespace(enabled=True)

    monkeypatch.setattr(speaker_lookups, "file_organization_id", _read("org", None))
    monkeypatch.setattr(speaker_lookups, "speaker_file_id", _read("file", 21))
    monkeypatch.setattr(workflow, "PrivacyView", _Privacy)
    monkeypatch.setattr(workflow, "touch_profile_interaction", _write("touch"))
    monkeypatch.setattr(workflow, "record_human_identity", _write("identity"))
    monkeypatch.setattr(speaker_identity_rules, "lift_profile_blacklist", _write("blacklist"))
    monkeypatch.setattr(speaker_cleanup, "profile_move_impact", _read("impact", SimpleNamespace()))

    def _rebuild(*_a) -> list[dict[str, Any]]:
        log.append("rebuild")
        return []

    monkeypatch.setattr(speaker_cleanup, "reconcile_speaker_impact", _rebuild)
    monkeypatch.setattr(
        opensearch_service, "update_speaker_profile", lambda **_kw: log.append("opensearch")
    )

    def _run(existing_profile: Any):
        monkeypatch.setattr(
            speaker_lookups, "profile_by_name_pattern", _read("profile", existing_profile)
        )
        speaker = SimpleNamespace(
            id=7, uuid="spk-7", user_id=3, media_file_id=21, profile_id=None, verified=False
        )
        workflow.auto_create_or_assign_profile(
            cast(Any, speaker), "Ada", cast(Session, _RecordingSession(log)), user_id=3
        )
        return log

    return _run


def _lock_scope_violations(log: list[str]) -> list[str]:
    """What ran while the speaker row was locked, or out of order; empty when the scope is short."""
    first_write = next(i for i, e in enumerate(log) if e.startswith(("write:", "flush")))
    if "commit" not in log[first_write:]:
        return ["nothing committed the label before the rebuild"]
    commit = log.index("commit", first_write)
    violations = [
        f"{e} while the speaker row was locked"
        for e in log[first_write:commit]
        if e.startswith("read:")
    ]
    violations += [
        f"{e} before the commit" for e in ("rebuild", "opensearch") if log.index(e) < commit
    ]
    return violations


def test_assigning_to_an_existing_profile_commits_before_any_read_or_rebuild(run):
    profile = SimpleNamespace(id=40, uuid="prof-40", name="Ada")

    log = run(profile)

    assert _lock_scope_violations(log) == [], log


def test_creating_a_profile_commits_before_any_read_or_rebuild(run):
    log = run(None)

    assert _lock_scope_violations(log) == [], log
