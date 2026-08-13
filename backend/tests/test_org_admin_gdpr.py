"""Tests for sub-step 6.2: org-admin audit-log read, GDPR erasure, org-admin guard.

Three concerns, all GPU-free and DB-backed via the savepoint ``db_session``:

* ``require_org_admin`` raises 403 for non-org-admins and passes for org-admins.
* The org-admin audit-log read is **scoped** to the caller's org members (it
  filters by the org's member user-ids; it can never see another org's events).
* ``erase_user`` / ``erase_organization`` remove the expected rows. Storage
  (MinIO) is mocked so these run without a live stack.
* An erasure that could NOT destroy every copy is reported as partial rather
  than as a success — see ``TestPartialErasureIsReportedAsPartial``.

Community invariance is asserted: a personal context (no org) is never an
org-admin, so the guard 403s and the org-scoped surfaces are unreachable.

**OpenSearch is a fake cluster, not a patched-out function.** The erasure code
under test — ``_erase_speaker_voiceprints`` and ``_cleanup_opensearch_for_file``
— runs in full against ``_FakeOpenSearch``. Patching those two out (which every
test here used to do) made ``voiceprints_deleted`` an assertion on the mock's own
return value: replacing the whole body of ``_erase_speaker_voiceprints`` with
``return 0`` failed no test in the repo, on the function whose docstring calls it
"the biometric-data guarantee the per-file path alone cannot make".
"""

import uuid as uuid_pkg
from contextlib import contextmanager
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError

from app.api.deps_context import RequestContext
from app.api.deps_context import require_org_admin
from app.auth.audit import AuditOutcome
from app.models.media import Collection
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.user import User


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _mk_user(db, label: str) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"{label}_{uid}@example.com",
        full_name=f"{label} user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_org(db, label: str) -> Organization:
    uid = str(uuid_pkg.uuid4())[:8]
    org = Organization(external_org_id=f"org_{label}_{uid}", name=f"{label} Org", is_active=True)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _mk_file(db, *, user: User, org_id: int | None) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=fuuid,
        filename=f"f_{str(fuuid)[:8]}.mp4",
        storage_path=f"media/test/{fuuid}.mp4",
        content_type="video/mp4",
        file_size=1000,
        user_id=user.id,
        organization_id=org_id,
        status="completed",
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


class World:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _unavailable(api: str) -> OpenSearchConnectionError:
    """The exception opensearch-py raises when the cluster cannot be reached.

    Built with opensearch-py's own three-argument shape (``status``, ``error``,
    ``info``) because ``ConnectionError.__str__`` reads ``args[1]`` and
    ``args[2]`` — a one-argument construction blows up on ``str(e)``, in the
    error-reporting code the test exists to exercise.
    """
    return OpenSearchConnectionError("N/A", f"{api}: connection refused", Exception("refused"))


class _FakeIndices:
    def __init__(self, cluster: "_FakeOpenSearch"):
        self._cluster = cluster

    def exists(self, index: str) -> bool:
        self._cluster.maybe_fail("indices.exists")
        return index not in self._cluster.absent_indices

    def exists_alias(self, name: str) -> bool:
        self._cluster.maybe_fail("indices.exists")
        return False


class _FakeOpenSearch:
    """A stand-in cluster whose answer to each API call the test chooses.

    OpenSearch itself is not the system under test — the app's behaviour when a
    store cannot *prove* the subject's documents are gone is. So the erasure
    functions run unmodified and talk to this.

    Args:
        deleted: ``deleted`` count each ``delete_by_query`` reports.
        counts: Per-index survivor counts returned by ``count`` (default 0).
        failures: ``failures`` list each ``delete_by_query`` reports — the way
            OpenSearch reports a *partial* sweep, in the body, without raising.
        fail: API names (``delete``, ``delete_by_query``, ``count``,
            ``indices.exists``) that raise "cluster unreachable" instead.
        absent_indices: Index names ``indices.exists`` answers False for.
    """

    def __init__(self, *, deleted=0, counts=None, failures=None, fail=(), absent_indices=()):
        self.deleted = deleted
        self.counts = counts or {}
        self.failures = list(failures or [])
        self.fail = set(fail)
        self.absent_indices = set(absent_indices)
        self.indices = _FakeIndices(self)
        self.deleted_docs: list[tuple[str, str]] = []
        self.delete_by_query_calls: list[tuple[str, dict]] = []

    def maybe_fail(self, api: str) -> None:
        if api in self.fail:
            raise _unavailable(api)

    def delete(self, index, id):  # noqa: A002 — opensearch-py's parameter name
        self.maybe_fail("delete")
        self.deleted_docs.append((index, str(id)))
        return {"result": "deleted"}

    def delete_by_query(self, index, body, refresh=False, conflicts=None):
        self.maybe_fail("delete_by_query")
        self.delete_by_query_calls.append((index, body))
        return {"deleted": self.deleted, "failures": self.failures}

    def count(self, index, body=None):
        self.maybe_fail("count")
        return {"count": self.counts.get(index, 0)}


@pytest.fixture()
def fake_opensearch():
    """Install a :class:`_FakeOpenSearch` at every attribute the erasure reads.

    Three, because the client is read three different ways: the package
    attribute (the erasure service and the file cleanup), the ``client``
    submodule global (``remove_speaker_embedding`` / ``remove_profile_embedding``
    and the index-existence helpers), and ``indexing_service``'s module-level
    import (``delete_transcript_chunks``). Patching only one leaves the others
    talking to the real dev cluster, which makes the test's result depend on
    whether the stack happens to be up.
    """

    @contextmanager
    def _install(**kwargs):
        cluster = _FakeOpenSearch(**kwargs)
        with (
            patch("app.services.opensearch_service.opensearch_client", cluster),
            patch("app.services.opensearch_service.client.opensearch_client", cluster),
            patch("app.services.search.indexing_service.opensearch_client", cluster),
        ):
            yield cluster

    return _install


@pytest.fixture()
def captured_audit():
    """Capture the audit events the erasure writes, without emitting them."""

    @contextmanager
    def _capture():
        fake = MagicMock()
        with patch("app.services.gdpr_erasure_service.audit_logger", fake):
            yield fake

    return _capture


def _speaker_indices() -> set[str]:
    """The speaker indices the voiceprint sweep visits (v3 + v4 + alias)."""
    from app.core.constants import get_speaker_index
    from app.core.constants import get_speaker_index_v3
    from app.core.constants import get_speaker_index_v4

    return {get_speaker_index(), get_speaker_index_v3(), get_speaker_index_v4()}


@pytest.fixture()
def two_orgs(db_session):
    """Two orgs, an admin member each, plus files/speakers/profiles/collections."""
    db = db_session
    org_a = _mk_org(db, "A")
    org_b = _mk_org(db, "B")
    admin_a = _mk_user(db, "admin_a")
    member_a = _mk_user(db, "member_a")
    admin_b = _mk_user(db, "admin_b")
    db.add_all(
        [
            OrganizationMembership(organization_id=org_a.id, user_id=admin_a.id, role="org:admin"),
            OrganizationMembership(
                organization_id=org_a.id, user_id=member_a.id, role="org:member"
            ),
            OrganizationMembership(organization_id=org_b.id, user_id=admin_b.id, role="org:admin"),
        ]
    )
    db.commit()

    file_a = _mk_file(db, user=member_a, org_id=org_a.id)
    speaker_a = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=member_a.id,
        organization_id=org_a.id,
        media_file_id=file_a.id,
        name="SPEAKER_00",
    )
    profile_a = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=member_a.id,
        organization_id=org_a.id,
        name=f"Profile {uuid_pkg.uuid4().hex[:6]}",
    )
    coll_a = Collection(
        uuid=uuid_pkg.uuid4(),
        user_id=member_a.id,
        organization_id=org_a.id,
        name=f"coll_{uuid_pkg.uuid4().hex[:6]}",
    )
    db.add_all([speaker_a, profile_a, coll_a])
    db.commit()

    return World(
        db=db,
        org_a=org_a,
        org_b=org_b,
        admin_a=admin_a,
        member_a=member_a,
        admin_b=admin_b,
        file_a=file_a,
        speaker_a=speaker_a,
        profile_a=profile_a,
        coll_a=coll_a,
    )


# --------------------------------------------------------------------------- #
# require_org_admin guard                                                      #
# --------------------------------------------------------------------------- #
class TestRequireOrgAdmin:
    def test_personal_context_is_403(self, two_orgs):
        """Community invariance: no org context -> not an org admin -> 403."""
        ctx = RequestContext(user=two_orgs.admin_a)  # org_id=None, org_role=None
        with pytest.raises(HTTPException) as exc:
            require_org_admin(ctx)
        assert exc.value.status_code == 403

    def test_org_member_is_403(self, two_orgs):
        """A plain org member is NOT an org admin."""
        ctx = RequestContext(
            user=two_orgs.member_a, org_id=two_orgs.org_a.id, org_role="org:member"
        )
        with pytest.raises(HTTPException) as exc:
            require_org_admin(ctx)
        assert exc.value.status_code == 403

    def test_org_admin_passes(self, two_orgs):
        """An org admin passes and the context is returned unchanged."""
        ctx = RequestContext(user=two_orgs.admin_a, org_id=two_orgs.org_a.id, org_role="org:admin")
        assert require_org_admin(ctx) is ctx


# --------------------------------------------------------------------------- #
# Org-admin audit-log read scoping                                            #
# --------------------------------------------------------------------------- #
class TestOrgAuditScope:
    def test_member_id_set_excludes_other_org(self, two_orgs):
        """The org-scope user-id set contains only the caller's org members."""
        from app.api.endpoints.org_admin import _org_member_user_ids

        ids_a = set(_org_member_user_ids(two_orgs.db, two_orgs.org_a.id))
        assert ids_a == {two_orgs.admin_a.id, two_orgs.member_a.id}
        assert two_orgs.admin_b.id not in ids_a  # org-B's admin is invisible

    def test_query_scoped_to_member_terms(self, two_orgs):
        """The org-admin read passes a ``terms`` filter on the org member ids;
        an org admin can never read another org's events."""
        from app.api.endpoints.org_admin import get_org_audit_logs

        ctx = RequestContext(user=two_orgs.admin_a, org_id=two_orgs.org_a.id, org_role="org:admin")
        captured = {}

        def _fake_query(**kwargs):
            captured.update(kwargs)
            return {"logs": [], "total": 0, "offset": 0, "limit": 100}

        with patch("app.api.endpoints.org_admin.query_audit_logs", _fake_query):
            # Direct call: pass the params FastAPI would resolve from Query(...).
            get_org_audit_logs(
                start_date=None,
                end_date=None,
                event_type=None,
                user_id=None,
                outcome=None,
                limit=100,
                offset=0,
                db=two_orgs.db,
                ctx=ctx,
            )

        assert set(captured["scope_user_ids"]) == {
            two_orgs.admin_a.id,
            two_orgs.member_a.id,
        }
        assert two_orgs.admin_b.id not in captured["scope_user_ids"]

    def test_user_id_filter_outside_org_is_403(self, two_orgs):
        """Filtering by a user_id outside the org is rejected (can't probe out)."""
        from app.api.endpoints.org_admin import get_org_audit_logs

        ctx = RequestContext(user=two_orgs.admin_a, org_id=two_orgs.org_a.id, org_role="org:admin")
        with pytest.raises(HTTPException) as exc:
            get_org_audit_logs(
                start_date=None,
                end_date=None,
                event_type=None,
                user_id=two_orgs.admin_b.id,
                outcome=None,
                limit=100,
                offset=0,
                db=two_orgs.db,
                ctx=ctx,
            )
        assert exc.value.status_code == 403

    def test_empty_scope_returns_nothing(self):
        """A scope of zero member-ids returns no events (not everything)."""
        from app.auth.audit import query_audit_logs

        with patch("app.auth.audit.settings") as s:
            s.AUDIT_LOG_TO_OPENSEARCH = True
            result = query_audit_logs(scope_user_ids=[])
        assert result["logs"] == []
        assert result["total"] == 0


# --------------------------------------------------------------------------- #
# Org attribution on audit events (issue #262a)                                #
# --------------------------------------------------------------------------- #
class _FakeAuditOS:
    """Captures the OpenSearch query body the audit read builds."""

    def __init__(self):
        self.captured_body = None

    def search(self, index, body):
        self.captured_body = body
        return {"hits": {"hits": [], "total": {"value": 0}}}


class TestAuditOrgAttribution:
    def test_log_event_carries_organization_id(self):
        """audit_logger.log(..., organization_id=N) stamps the event payload."""
        import json
        from unittest.mock import MagicMock

        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditOutcome
        from app.auth.audit import audit_logger

        fake_logger = MagicMock()
        with (
            patch("app.auth.audit.settings") as s,
            patch.object(audit_logger, "_logger", fake_logger),
        ):
            s.AUDIT_LOG_ENABLED = True
            s.AUDIT_LOG_TO_OPENSEARCH = False
            s.AUDIT_LOG_FORMAT = "json"
            audit_logger.log(
                event_type=AuditEventType.ADMIN_FILE_QUARANTINE,
                outcome=AuditOutcome.SUCCESS,
                user_id=7,
                username="a@example.com",
                organization_id=42,
            )

        event = json.loads(fake_logger.info.call_args[0][0])
        assert event["organization_id"] == 42
        assert event["user_id"] == 7

    def test_log_event_defaults_to_no_org(self):
        """No-context writers (e.g. local login) emit organization_id=None —
        the legacy/member-attributed class of events."""
        import json
        from unittest.mock import MagicMock

        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditOutcome
        from app.auth.audit import audit_logger

        fake_logger = MagicMock()
        with (
            patch("app.auth.audit.settings") as s,
            patch.object(audit_logger, "_logger", fake_logger),
        ):
            s.AUDIT_LOG_ENABLED = True
            s.AUDIT_LOG_TO_OPENSEARCH = False
            s.AUDIT_LOG_FORMAT = "json"
            audit_logger.log(
                event_type=AuditEventType.AUTH_LOGIN_SUCCESS,
                outcome=AuditOutcome.SUCCESS,
                user_id=7,
            )

        event = json.loads(fake_logger.info.call_args[0][0])
        assert event["organization_id"] is None

    def test_org_scope_clause_shape(self):
        """The org-admin visibility clause: org-stamped events (incl. user_id
        NULL) OR legacy un-stamped events attributed via member ids. An event
        stamped with a DIFFERENT org matches neither branch."""
        from app.auth.audit import build_org_scope_clause

        clause = build_org_scope_clause(42, [1, 2])
        assert clause == {
            "bool": {
                "should": [
                    {"term": {"organization_id": 42}},
                    {
                        "bool": {
                            "must_not": [{"exists": {"field": "organization_id"}}],
                            "filter": [{"terms": {"user_id": [1, 2]}}],
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }

    def test_get_org_audit_logs_passes_org_scope(self, two_orgs):
        """The org-admin endpoint threads BOTH the member set (legacy events)
        and its own org id (stamped events, incl. failed logins with user_id
        NULL) into the shared query."""
        from app.api.endpoints.org_admin import get_org_audit_logs

        ctx = RequestContext(user=two_orgs.admin_a, org_id=two_orgs.org_a.id, org_role="org:admin")
        captured = {}

        def _fake_query(**kwargs):
            captured.update(kwargs)
            return {"logs": [], "total": 0, "offset": 0, "limit": 100}

        with patch("app.api.endpoints.org_admin.query_audit_logs", _fake_query):
            get_org_audit_logs(
                start_date=None,
                end_date=None,
                event_type=None,
                user_id=None,
                outcome=None,
                limit=100,
                offset=0,
                db=two_orgs.db,
                ctx=ctx,
            )

        assert captured["scope_org_id"] == two_orgs.org_a.id
        assert set(captured["scope_user_ids"]) == {two_orgs.admin_a.id, two_orgs.member_a.id}

    def test_query_audit_logs_builds_org_scope_body(self):
        """query_audit_logs(scope_org_id=...) embeds the org-scope clause in
        the OpenSearch bool query."""
        from app.auth.audit import build_org_scope_clause
        from app.auth.audit import query_audit_logs

        fake = _FakeAuditOS()
        with (
            patch("app.auth.audit.settings") as s,
            patch("app.auth.audit._build_audit_opensearch_client", return_value=fake),
        ):
            s.AUDIT_LOG_TO_OPENSEARCH = True
            result = query_audit_logs(scope_user_ids=[1, 2], scope_org_id=42)

        assert result["total"] == 0
        assert fake.captured_body is not None
        must = fake.captured_body["query"]["bool"]["must"]
        assert build_org_scope_clause(42, [1, 2]) in must
        # The bare member-terms filter must NOT also be applied — it would
        # exclude org-stamped events with user_id NULL (failed logins).
        assert {"terms": {"user_id": [1, 2]}} not in must

    def test_org_scope_with_empty_member_set_still_queries(self):
        """With an org id, an empty member set must NOT short-circuit — the
        org's STAMPED events (e.g. failed logins, user_id NULL) stay visible."""
        from app.auth.audit import query_audit_logs

        fake = _FakeAuditOS()
        with (
            patch("app.auth.audit.settings") as s,
            patch("app.auth.audit._build_audit_opensearch_client", return_value=fake),
        ):
            s.AUDIT_LOG_TO_OPENSEARCH = True
            query_audit_logs(scope_user_ids=[], scope_org_id=42)

        assert fake.captured_body is not None  # query executed, not short-circuited
        must = fake.captured_body["query"]["bool"]["must"]
        assert {"term": {"organization_id": 42}} in must[0]["bool"]["should"]

    def test_member_only_scope_unchanged(self):
        """Without an org id the pre-existing member-terms scoping is intact
        (super-admin/global callers are unaffected)."""
        from app.auth.audit import query_audit_logs

        fake = _FakeAuditOS()
        with (
            patch("app.auth.audit.settings") as s,
            patch("app.auth.audit._build_audit_opensearch_client", return_value=fake),
        ):
            s.AUDIT_LOG_TO_OPENSEARCH = True
            query_audit_logs(scope_user_ids=[1, 2])

        assert fake.captured_body is not None
        assert {"terms": {"user_id": [1, 2]}} in fake.captured_body["query"]["bool"]["must"]

    def test_request_org_id_only_trusts_resolved_int(self):
        """request.state.org_id may transiently hold the provider's RAW string
        (pre-membership-mirror); only the refined int is a valid audit stamp."""
        from types import SimpleNamespace

        from app.auth.audit import request_org_id

        assert request_org_id(SimpleNamespace(state=SimpleNamespace(org_id=5))) == 5
        assert request_org_id(SimpleNamespace(state=SimpleNamespace(org_id="org_raw123"))) is None
        assert request_org_id(SimpleNamespace(state=SimpleNamespace())) is None

    def test_gdpr_org_erasure_events_are_org_stamped(self, two_orgs):
        """The org-admin GDPR erasure audit events carry the tenant id."""
        w = two_orgs
        calls = []

        def _capture(**kwargs):
            calls.append(kwargs)

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            patch(
                "app.services.file_cleanup_service._cleanup_opensearch_for_file",
                return_value=[],
            ),
            patch(
                "app.services.gdpr_erasure_service._erase_speaker_voiceprints",
                return_value=0,
            ),
            patch(
                "app.services.opensearch_service.remove_profile_embedding",
                return_value=True,
            ),
            patch("app.services.gdpr_erasure_service.audit_logger") as fake_audit,
        ):
            fake_audit.log.side_effect = _capture
            from app.services.gdpr_erasure_service import erase_org_member_data

            erase_org_member_data(
                w.db,
                int(w.member_a.id),
                int(w.org_a.id),
                actor_user_id=int(w.admin_a.id),
                actor_email=str(w.admin_a.email),
            )

        assert calls, "erasure must audit"
        assert calls[-1]["organization_id"] == w.org_a.id


# --------------------------------------------------------------------------- #
# GDPR erasure                                                                 #
# --------------------------------------------------------------------------- #
class TestEraseUser:
    def test_erase_user_removes_rows(self, two_orgs, fake_opensearch):
        """erase_user removes the user's files, speaker, profile, collection,
        membership, and the user row itself.

        Only object storage is patched. The OpenSearch work is real code against
        a fake cluster, so ``voiceprints_deleted`` is a count this code computed
        rather than a number handed to it by a mock.
        """
        db = two_orgs.db
        member_id = two_orgs.member_a.id
        expected_voiceprints = len(_speaker_indices())  # one delete_by_query each

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            fake_opensearch(deleted=1) as cluster,
        ):
            from app.services.gdpr_erasure_service import erase_user

            summary = erase_user(db, member_id)

        assert summary["users_deleted"] == 1
        assert summary["media_files_deleted"] == 1
        assert summary["speaker_profiles_deleted"] == 1
        assert summary["collections_deleted"] == 1
        assert summary["voiceprints_deleted"] == expected_voiceprints
        assert summary["sla_days"] == 30
        assert summary["errors"] == []
        assert summary["complete"] is True

        # The sweep was scoped to this user, and the transcript document for the
        # user's file was deleted by uuid — the two things the counter can't say.
        scoped = [body for idx, body in cluster.delete_by_query_calls if idx in _speaker_indices()]
        assert scoped, "the voiceprint sweep must run"
        for body in scoped:
            assert {"term": {"user_id": member_id}} in body["query"]["bool"]["filter"]
        assert str(two_orgs.file_a.uuid) in [doc_id for _idx, doc_id in cluster.deleted_docs]

        # Rows are gone.
        assert db.query(User).filter(User.id == member_id).first() is None
        assert db.query(MediaFile).filter(MediaFile.user_id == member_id).count() == 0
        assert db.query(SpeakerProfile).filter(SpeakerProfile.user_id == member_id).count() == 0
        assert (
            db.query(OrganizationMembership)
            .filter(OrganizationMembership.user_id == member_id)
            .count()
            == 0
        )

    def test_erase_user_idempotent(self, two_orgs):
        """Erasing a non-existent user id is a no-op with zeroed counters."""
        from app.services.gdpr_erasure_service import erase_user

        summary = erase_user(two_orgs.db, 999_999_999)
        assert summary["already_erased"] is True
        assert summary["users_deleted"] == 0


class TestEraseOrganization:
    def test_erase_org_removes_org_data(self, two_orgs, fake_opensearch):
        """erase_organization removes org files/profiles/collections + the org row,
        but keeps the member's user account."""
        db = two_orgs.db
        org_id = two_orgs.org_a.id
        member_id = two_orgs.member_a.id
        expected_voiceprints = len(_speaker_indices())

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            fake_opensearch(deleted=1) as cluster,
        ):
            from app.services.gdpr_erasure_service import erase_organization

            summary = erase_organization(db, org_id)

        assert summary["media_files_deleted"] == 1
        assert summary["speaker_profiles_deleted"] == 1
        assert summary["collections_deleted"] == 1
        assert summary["voiceprints_deleted"] == expected_voiceprints
        assert summary["memberships_removed"] >= 1
        assert summary["errors"] == []
        assert summary["complete"] is True

        # Org erasure scopes the biometric sweep by organization_id, not user id.
        scoped = [body for idx, body in cluster.delete_by_query_calls if idx in _speaker_indices()]
        assert scoped, "the voiceprint sweep must run"
        for body in scoped:
            assert {"term": {"organization_id": org_id}} in body["query"]["bool"]["filter"]

        # Org + its org-scoped data gone; member user survives.
        assert db.query(Organization).filter(Organization.id == org_id).first() is None
        assert db.query(MediaFile).filter(MediaFile.organization_id == org_id).count() == 0
        assert (
            db.query(OrganizationMembership)
            .filter(OrganizationMembership.organization_id == org_id)
            .count()
            == 0
        )
        assert db.query(User).filter(User.id == member_id).first() is not None

    def test_erase_org_idempotent(self, two_orgs):
        from app.services.gdpr_erasure_service import erase_organization

        summary = erase_organization(two_orgs.db, 999_999_999)
        assert summary["already_erased"] is True


# --------------------------------------------------------------------------- #
# Org-scoped member erasure + legal-hold guard (0.5.0 review fixes)            #
# --------------------------------------------------------------------------- #
class TestOrgScopedErasure:
    def test_erase_org_member_data_is_org_scoped(self, two_orgs):
        """The org-admin erasure destroys ONLY the target's rows in that org —
        personal-scope files, other-org files, and the account survive."""
        w = two_orgs
        db = w.db
        personal_file = _mk_file(db, user=w.member_a, org_id=None)
        other_org_file = _mk_file(db, user=w.member_a, org_id=w.org_b.id)

        from app.services.gdpr_erasure_service import erase_org_member_data

        summary = erase_org_member_data(
            db,
            int(w.member_a.id),
            int(w.org_a.id),
            actor_user_id=int(w.admin_a.id),
            actor_email=str(w.admin_a.email),
        )

        assert summary["media_files_deleted"] == 1  # only file_a (org A)
        assert db.query(MediaFile).filter(MediaFile.id == personal_file.id).first() is not None
        assert db.query(MediaFile).filter(MediaFile.id == other_org_file.id).first() is not None
        assert db.query(MediaFile).filter(MediaFile.id == w.file_a.id).first() is None
        # Org-scoped profile/collection gone; the ACCOUNT survives.
        assert db.query(SpeakerProfile).filter(SpeakerProfile.id == w.profile_a.id).first() is None
        assert db.query(Collection).filter(Collection.id == w.coll_a.id).first() is None
        assert db.query(User).filter(User.id == w.member_a.id).first() is not None

    def test_erase_org_member_data_records_voiceprint_failure(self, two_orgs, fake_opensearch):
        """The org-scoped erasure reports a failed biometric sweep too — the
        (user, org) voiceprint docs are the only copy no relational CASCADE
        reaches."""
        w = two_orgs
        from app.services.gdpr_erasure_service import erase_org_member_data

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            fake_opensearch(fail=("delete_by_query",)),
        ):
            summary = erase_org_member_data(w.db, int(w.member_a.id), int(w.org_a.id))

        assert summary["complete"] is False
        assert any(e.get("stage") == "voiceprints" for e in summary["errors"])

    def test_erase_user_skips_legal_hold_files(self, two_orgs):
        """GDPR Art. 17(3)(e): files under an active legal hold are preserved and
        reported; the user row is retained until the hold releases."""
        w = two_orgs
        db = w.db
        held = _mk_file(db, user=w.member_a, org_id=None)
        held.legal_hold = True
        held.is_quarantined = True
        db.commit()

        from app.services.gdpr_erasure_service import erase_user

        summary = erase_user(db, int(w.member_a.id))

        assert summary["legal_holds_skipped"] == 1
        assert db.query(MediaFile).filter(MediaFile.id == held.id).first() is not None
        # Non-held file was erased; account retained because a hold remains.
        assert db.query(MediaFile).filter(MediaFile.id == w.file_a.id).first() is None
        assert summary["users_deleted"] == 0
        assert db.query(User).filter(User.id == w.member_a.id).first() is not None


# --------------------------------------------------------------------------- #
# Partial erasure must be REPORTED as partial                                  #
# --------------------------------------------------------------------------- #
def _mk_file_with_speaker(db, user: User) -> tuple[MediaFile, Speaker]:
    """A media file with one speaker — the shape the OpenSearch sweep walks."""
    media = _mk_file(db, user=user, org_id=None)
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        media_file_id=media.id,
        name="SPEAKER_00",
    )
    db.add(speaker)
    db.commit()
    return media, speaker


def _stages(summary) -> list[str]:
    return [e.get("stage") for e in summary["errors"]]


def _outcome(fake_audit) -> AuditOutcome:
    assert fake_audit.log.call_args is not None, "the erasure must audit"
    # `call_args.kwargs` is Any; cast rather than widen the return type, so callers still
    # get the enum and a wrong-outcome assertion stays type-checked.
    return cast(AuditOutcome, fake_audit.log.call_args.kwargs["outcome"])


class TestPartialErasureIsReportedAsPartial:
    """An Art. 17 erasure that could not destroy every copy must not be recorded
    as a completed one.

    The failure this class exists for: OpenSearch is briefly unreachable during
    an erasure. Every step was wrapped in ``contextlib.suppress(Exception)``, so
    the DB rows and the account were destroyed while the verbatim transcript and
    its RAG chunks stayed indexed and searchable — and the caller got
    ``errors: []``, the audit log said SUCCESS, and the row that would identify
    what to re-delete was already gone. Silence is the defect; each test below
    breaks one store and asserts the erasure says so.

    One failing store must still not abort the others: the relational and
    storage deletions are the legally-binding ones, so every test also asserts
    the rows really went.
    """

    # -- voiceprints (biometric data) --------------------------------------- #
    def test_healthy_erasure_still_audits_success(
        self, db_session, fake_opensearch, captured_audit
    ):
        """The control for every test below: same code path, healthy cluster,
        SUCCESS. Without it, a PARTIAL-always bug would pass the whole class."""
        from app.services.gdpr_erasure_service import erase_user

        user = _mk_user(db_session, "healthy")
        _mk_file_with_speaker(db_session, user)

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            fake_opensearch(deleted=2),
            captured_audit() as fake_audit,
        ):
            summary = erase_user(db_session, user.id)

        assert summary["errors"] == []
        assert summary["complete"] is True
        assert summary["voiceprints_deleted"] == 2 * len(_speaker_indices())
        assert _outcome(fake_audit) is AuditOutcome.SUCCESS

    def test_voiceprint_sweep_failure_blocks_a_success_audit(
        self, db_session, fake_opensearch, captured_audit
    ):
        """Biometric data surviving an Art. 17 request is not a completed
        erasure. The user has no files, so the voiceprint sweep is the only
        store that can fail — the error cannot have come from anywhere else."""
        from app.services.gdpr_erasure_service import erase_user

        user = _mk_user(db_session, "vp_fail")

        with (
            fake_opensearch(fail=("delete_by_query",)),
            captured_audit() as fake_audit,
        ):
            summary = erase_user(db_session, user.id)

        assert _stages(summary) == ["voiceprints"] * len(_speaker_indices())
        assert summary["complete"] is False
        assert _outcome(fake_audit) is AuditOutcome.PARTIAL
        # ...and the erasure still ran to completion rather than aborting.
        assert summary["users_deleted"] == 1
        assert db_session.query(User).filter(User.id == user.id).first() is None

    def test_unavailable_opensearch_client_is_not_a_silent_success(
        self, db_session, captured_audit
    ):
        """No client at all is the same finding as a failing one: the biometric
        docs were never asked about, so they are not known to be gone."""
        from app.services.gdpr_erasure_service import erase_user

        user = _mk_user(db_session, "vp_noclient")

        with (
            patch("app.services.opensearch_service.opensearch_client", None),
            patch("app.services.opensearch_service.client.opensearch_client", None),
            patch("app.services.search.indexing_service.opensearch_client", None),
            captured_audit() as fake_audit,
        ):
            summary = erase_user(db_session, user.id)

        assert _stages(summary) == ["voiceprints"]
        assert "unavailable" in summary["errors"][0]["error"]
        assert summary["complete"] is False
        assert _outcome(fake_audit) is AuditOutcome.PARTIAL

    def test_partial_delete_by_query_reported_in_the_body_is_recorded(
        self, db_session, fake_opensearch, captured_audit
    ):
        """``delete_by_query`` reports per-document failures in the RESPONSE
        BODY and returns 200. Reading only ``deleted`` makes a sweep that left
        documents behind indistinguishable from one that did not."""
        from app.services.gdpr_erasure_service import erase_user

        user = _mk_user(db_session, "vp_partial")

        with (
            fake_opensearch(deleted=1, failures=[{"cause": "version_conflict"}]),
            captured_audit() as fake_audit,
        ):
            summary = erase_user(db_session, user.id)

        assert _stages(summary) == ["voiceprints"] * len(_speaker_indices())
        assert summary["complete"] is False
        assert _outcome(fake_audit) is AuditOutcome.PARTIAL

    # -- transcript text + RAG chunks --------------------------------------- #
    def test_transcript_document_failure_blocks_a_success_audit(
        self, db_session, fake_opensearch, captured_audit
    ):
        """The transcript document is the verbatim text. Losing the DB row while
        it stays indexed leaves it searchable with nothing left to point at it."""
        from app.services.gdpr_erasure_service import erase_user

        user = _mk_user(db_session, "tx_fail")
        media, _speaker = _mk_file_with_speaker(db_session, user)

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            fake_opensearch(fail=("delete",)),
            captured_audit() as fake_audit,
        ):
            summary = erase_user(db_session, user.id)

        assert "transcript" in _stages(summary)
        assert summary["complete"] is False
        assert _outcome(fake_audit) is AuditOutcome.PARTIAL
        # The DB deletion still happened — partial, not aborted halfway.
        assert summary["media_files_deleted"] == 1
        assert db_session.query(MediaFile).filter(MediaFile.id == media.id).first() is None

    def test_surviving_chunks_are_reported_although_the_delete_reported_success(
        self, db_session, fake_opensearch, captured_audit
    ):
        """``delete_transcript_chunks`` returns 0 for "no chunks" AND for "the
        delete failed", so its return value cannot tell them apart. The count of
        chunks still matching the file is what proves the RAG index is clean —
        and this is the exact scenario the erasure used to call a success."""
        from app.core.config import settings
        from app.services.gdpr_erasure_service import erase_user

        user = _mk_user(db_session, "chunks_left")
        _mk_file_with_speaker(db_session, user)

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            fake_opensearch(counts={settings.OPENSEARCH_CHUNKS_INDEX: 4}),
            captured_audit() as fake_audit,
        ):
            summary = erase_user(db_session, user.id)

        assert _stages(summary) == ["transcript_chunks"]
        assert "4 chunk(s) survive" in summary["errors"][0]["error"]
        assert summary["complete"] is False
        assert _outcome(fake_audit) is AuditOutcome.PARTIAL

    def test_an_unverifiable_index_is_not_treated_as_an_empty_one(
        self, db_session, fake_opensearch, captured_audit
    ):
        """ "I could not ask" is not "nothing is there". A cluster that cannot
        answer the verification query leaves both the voiceprint docs and the
        RAG chunks unproven, and both must be reported."""
        from app.services.gdpr_erasure_service import erase_user

        user = _mk_user(db_session, "unverifiable")
        _mk_file_with_speaker(db_session, user)

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            fake_opensearch(fail=("count",)),
            captured_audit() as fake_audit,
        ):
            summary = erase_user(db_session, user.id)

        assert "speakers" in _stages(summary)
        assert "transcript_chunks" in _stages(summary)
        assert summary["complete"] is False
        assert _outcome(fake_audit) is AuditOutcome.PARTIAL

    def test_summary_index_failure_is_recorded(self, db_session, fake_opensearch, captured_audit):
        """Summaries are LLM-written prose about the recording; they are as much
        the subject's data as the transcript is."""
        from app.services.gdpr_erasure_service import erase_user

        user = _mk_user(db_session, "summ_fail")
        _mk_file_with_speaker(db_session, user)

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            # The summary sweep is the only delete_by_query in the per-file path;
            # the failures list also trips the voiceprint sweep, hence the
            # membership assertion rather than an equality one.
            fake_opensearch(failures=[{"cause": "rejected"}]),
            captured_audit() as fake_audit,
        ):
            summary = erase_user(db_session, user.id)

        assert "transcript_summaries" in _stages(summary)
        assert summary["complete"] is False
        assert _outcome(fake_audit) is AuditOutcome.PARTIAL

    # -- object storage ------------------------------------------------------ #
    def test_object_storage_failure_blocks_a_success_audit(
        self, db_session, fake_opensearch, captured_audit
    ):
        """The media file itself. The DB row goes either way, so a swallowed
        storage error leaves the recording in the bucket with nothing in the
        application that knows it is there."""
        from app.services.gdpr_erasure_service import erase_user

        user = _mk_user(db_session, "minio_fail")
        _mk_file_with_speaker(db_session, user)

        def _boom(_object_name):
            raise RuntimeError("MinIO: connection refused")

        with (
            patch("app.services.minio_service.delete_file", _boom),
            patch(
                "app.services.video_processing_service."
                "VideoProcessingService.clear_cache_for_media_file",
                return_value=None,
            ),
            fake_opensearch(),
            captured_audit() as fake_audit,
        ):
            summary = erase_user(db_session, user.id)

        assert _stages(summary) == ["storage"]
        assert summary["complete"] is False
        assert _outcome(fake_audit) is AuditOutcome.PARTIAL
        assert summary["media_files_deleted"] == 1  # the DB row still went

    def test_object_storage_success_is_the_control(
        self, db_session, fake_opensearch, captured_audit
    ):
        """Identical to the test above with one variable changed — the storage
        delete works — so the PARTIAL there is caused by the failure and not by
        the fixture."""
        from app.services.gdpr_erasure_service import erase_user

        user = _mk_user(db_session, "minio_ok")
        _mk_file_with_speaker(db_session, user)

        with (
            patch("app.services.minio_service.delete_file", lambda _object_name: None),
            patch(
                "app.services.video_processing_service."
                "VideoProcessingService.clear_cache_for_media_file",
                return_value=None,
            ),
            fake_opensearch(),
            captured_audit() as fake_audit,
        ):
            summary = erase_user(db_session, user.id)

        assert summary["errors"] == []
        assert summary["complete"] is True
        assert _outcome(fake_audit) is AuditOutcome.SUCCESS

    # -- the per-file contract the erasure depends on ------------------------ #
    def test_purge_media_file_reports_residual_errors_beside_a_deleted_row(
        self, db_session, fake_opensearch
    ):
        """``deleted: True`` means the DATABASE ROW went, not that every copy
        did. Callers that read only ``deleted`` — the erasure did — cannot see
        an incomplete destroy, so the residual list is part of the contract."""
        from app.services.file_cleanup_service import purge_media_file

        user = _mk_user(db_session, "purge_residual")
        media, _speaker = _mk_file_with_speaker(db_session, user)

        with (
            patch(
                "app.services.file_cleanup_service.delete_file_storage_artifacts",
                return_value=True,
            ),
            fake_opensearch(fail=("delete", "count")),
        ):
            result = purge_media_file(db_session, media)

        assert result["deleted"] is True
        assert result["error"] is None
        assert {e["stage"] for e in result["residual_errors"]} >= {"transcript", "speakers"}
