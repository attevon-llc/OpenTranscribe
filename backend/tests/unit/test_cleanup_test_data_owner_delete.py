"""Owner-delete for rows owned by other registered test users (issue #1237).

The sweep deletes every record through the ADMIN session, and the admin is not authorised on
another user's speaker profile (403), so a leaked ``delres-e2e-<hex8>@example.com`` account's
rows were never removed, the account stayed ``BLOCKED`` by them, and the log said nothing.
Registered test owners are now deleted through ``DELETE /api/admin/users/{uuid}`` (which
cascades their data), and any failed delete prints its HTTP status and reason.

Unit-only: a fake HTTP session and a fake DB connection; nothing here reaches a stack.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _load("cleanup_test_data_owner_delete", _REPO / "scripts" / "cleanup-test-data.py")
users = cleanup._load_cleanup_users_module()

OWNER = "delres-e2e-0a1b2c3d@example.com"
OWNER_UUID = "11111111-2222-7333-8444-555555555555"
PROFILE_UUID = "99999999-2222-7333-8444-555555555555"


class _Resp:
    def __init__(self, status: int, body: Any = None, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text
        self.ok = status < 400
        self.reason = {403: "Forbidden", 500: "Internal Server Error"}.get(status, "")

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _FakeSession:
    """Admin session stand-in: another user's profile is 403, the admin user delete works."""

    def __init__(self, user_delete_status: int = 200) -> None:
        self.deleted: list[str] = []
        self.user_delete_status = user_delete_status

    def delete(self, url: str, **_: Any) -> _Resp:
        self.deleted.append(url)
        if "/api/admin/users/" in url:
            return _Resp(self.user_delete_status, {"detail": "boom"})
        return _Resp(403, {"detail": "Not authorized to access this profile"})


def _api(session: _FakeSession) -> Any:
    api = cleanup.ApiSession("http://stack.invalid")
    api.session = session
    return api


def _profile(owner: str | None = OWNER) -> Any:
    return cleanup.Candidate("speaker_profile", PROFILE_UUID, "e2e-delres-0a1b2c3d", owner)


@pytest.mark.unit
def test_a_leaked_profile_owned_by_a_registered_test_user_is_deleted_via_its_owner() -> None:
    session = _FakeSession()
    api = _api(session)
    by_resource = {"speaker_profile": [_profile()]}

    owners = cleanup.owners_to_delete(by_resource, {OWNER: OWNER_UUID})
    assert owners == {OWNER: OWNER_UUID}

    outcome = cleanup._delete_owners(api, owners)
    deleted, failed = cleanup._delete_candidates(
        api, "speaker_profile", by_resource["speaker_profile"], outcome
    )

    assert (deleted, failed) == (1, False)
    assert session.deleted == [f"http://stack.invalid/api/admin/users/{OWNER_UUID}"]


@pytest.mark.unit
def test_a_candidate_whose_owner_is_not_a_deletable_test_user_is_left_to_the_api_path() -> None:
    session = _FakeSession()
    api = _api(session)
    # admin@example.com is not a registered shape, so it is never in the deletable map.
    cand = _profile("admin@example.com")

    assert cleanup.owners_to_delete({"speaker_profile": [cand]}, {OWNER: OWNER_UUID}) == {}
    assert cleanup._delete_owners(api, {}) == {}
    cleanup._delete_candidates(api, "speaker_profile", [cand], {})

    assert session.deleted == [f"http://stack.invalid/api/speaker-profiles/profiles/{PROFILE_UUID}"]


@pytest.mark.unit
def test_a_human_named_profile_is_never_a_candidate_so_its_owner_is_never_pulled_in() -> None:
    # Owner deletion is only ever reached from a candidate; a human's profile is not one.
    assert not cleanup._name_matches_shape(
        "Alice voice", cleanup.NAME_PREFIXES["speaker_profile"], "speaker_profile"
    )
    assert cleanup.owners_to_delete({"speaker_profile": []}, {OWNER: OWNER_UUID}) == {}


@pytest.mark.unit
def test_a_failed_owner_delete_prints_status_and_reason_and_counts_the_row_failed(capsys) -> None:
    api = _api(_FakeSession(user_delete_status=500))
    cand = _profile()

    outcome = cleanup._delete_owners(api, {OWNER: OWNER_UUID})
    deleted, failed = cleanup._delete_candidates(api, "speaker_profile", [cand], outcome)

    assert (deleted, failed) == (0, True)
    out = capsys.readouterr().out
    assert "HTTP 500" in out
    assert "boom" in out
    assert OWNER in out


@pytest.mark.unit
def test_a_failed_plain_delete_prints_http_status_and_reason(capsys) -> None:
    api = _api(_FakeSession())

    deleted, failed = cleanup._delete_candidates(api, "speaker_profile", [_profile("x@y")], {})

    assert (deleted, failed) == (0, True)
    out = capsys.readouterr().out
    assert "HTTP 403" in out
    assert "Not authorized to access this profile" in out
    assert PROFILE_UUID in out


@pytest.mark.unit
def test_failure_text_falls_back_to_the_body_and_is_bounded() -> None:
    assert cleanup.describe_failure(_Resp(500, text="x" * 1000)).startswith("HTTP 500")
    assert len(cleanup.describe_failure(_Resp(500, text="x" * 1000))) < 300
    assert cleanup.describe_failure(_Resp(403, {"detail": "nope"})) == "HTTP 403 Forbidden: nope"


class _Row:
    def __init__(self, uuid: str, email: str) -> None:
        self.uuid = uuid
        self.email = email


class _FakeConn:
    def __init__(self, rows: list[_Row]) -> None:
        self.rows = rows
        self.sql = ""
        self.params: dict[str, Any] = {}

    def execute(self, stmt: Any, params: dict[str, Any]) -> Any:
        self.sql, self.params = str(stmt), params
        rows = self.rows
        return type("R", (), {"fetchall": lambda _self: rows})()


@pytest.mark.unit
def test_deletable_owners_are_registered_shapes_older_than_the_cutoff_and_never_kept() -> None:
    conn = _FakeConn([_Row(OWNER_UUID, OWNER), _Row("kept-uuid", "admin@example.com")])

    found = cleanup.find_deletable_test_owners(conn, 1234, users.ORPHAN_PATTERNS_UNAMBIGUOUS)

    assert found == {OWNER: OWNER_UUID}  # a KEEP_EMAILS row is refused even if SQL returned it
    assert "created_at < to_timestamp(:cutoff)" in conn.sql
    assert conn.params["cutoff"] == 1234
    assert "delres-e2e-%@example.com" in conn.sql
    assert "testuser" not in conn.sql  # review-tier shapes are not in the Tier A pattern list


@pytest.mark.unit
def test_no_patterns_means_no_query_and_no_owners() -> None:
    conn = _FakeConn([_Row(OWNER_UUID, OWNER)])
    assert cleanup.find_deletable_test_owners(conn, 1, []) == {}
    assert conn.sql == ""


@pytest.mark.unit
def test_review_tier_owners_need_the_full_execute() -> None:
    assert cleanup.owner_patterns(users, include_tier_b=False) == users.ORPHAN_PATTERNS_UNAMBIGUOUS
    assert cleanup.owner_patterns(users, include_tier_b=True) == users.ORPHAN_PATTERNS
