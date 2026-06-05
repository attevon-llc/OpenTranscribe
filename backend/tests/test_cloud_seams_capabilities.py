"""Tests for capabilities/entitlements, pipeline hooks, request context, and
usage-event recording (cloud-edition seams, parts 3+4).

Community-default behavior is the contract: everything on except cloud-only
surfaces, hooks are no-ops, and a broken cloud layer can never break core.
"""

import uuid
from types import SimpleNamespace
from typing import Any
from typing import Optional

import pytest
from fastapi import HTTPException
from fastapi import Request

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.deps_context import scope_to_context
from app.auth.external_sync import sync_external_user_to_db
from app.auth.provider_registry import ExternalIdentity
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import capability_enabled
from app.core.capabilities import get_capabilities
from app.core.capabilities import require_capability
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.services.usage_service import record_event
from app.tasks.transcription.hooks import CompletionContext
from app.tasks.transcription.hooks import DispatchContext
from app.tasks.transcription.hooks import QuotaExceededError
from app.tasks.transcription.hooks import clear_hooks
from app.tasks.transcription.hooks import fire_before_dispatch
from app.tasks.transcription.hooks import fire_transcription_complete
from app.tasks.transcription.hooks import register_before_dispatch
from app.tasks.transcription.hooks import register_transcription_complete


def _fake_request(**state: Any) -> Request:
    return SimpleNamespace(state=SimpleNamespace(**state))  # type: ignore[return-value]


def _identity(**overrides: Any) -> ExternalIdentity:
    defaults: dict[str, Any] = {
        "provider": "clerk",
        "external_id": f"user_{uuid.uuid4().hex[:12]}",
        "email": f"cap-test-{uuid.uuid4().hex[:8]}@example.com",
        "full_name": "Cap Tester",
        "org_id": f"org_{uuid.uuid4().hex[:10]}",
        "org_role": "org:member",
    }
    defaults.update(overrides)
    return ExternalIdentity(**defaults)


@pytest.fixture(autouse=True)
def clean_seams():
    reset_capability_resolver()
    clear_hooks()
    yield
    reset_capability_resolver()
    clear_hooks()


class TestCapabilities:
    def test_community_defaults_everything_on_except_cloud_surfaces(self):
        caps = get_capabilities()
        assert caps["watch_sources"] is True
        assert caps["engine.settings"] is True
        assert caps["auth.config_ui"] is True
        # Cloud-only surfaces don't exist without the cloud layer
        assert caps["billing"] is False
        assert caps["usage_dashboard"] is False
        assert caps["organizations"] is False

    def test_resolver_override_and_reset(self):
        set_capability_resolver(lambda _req: {"watch_sources": False, "billing": True})
        caps = get_capabilities()
        assert caps["watch_sources"] is False
        assert caps["billing"] is True
        # Partial resolvers can't accidentally disable unconsidered surfaces
        assert caps["engine.settings"] is True

        reset_capability_resolver()
        assert get_capabilities() == COMMUNITY_CAPABILITIES

    def test_capability_enabled_unknown_key_false(self):
        assert capability_enabled("does.not.exist") is False

    def test_require_capability_404_when_off(self):
        dep = require_capability("billing")  # off in community
        with pytest.raises(HTTPException) as exc:
            dep(_fake_request())
        assert exc.value.status_code == 404

        dep_on = require_capability("watch_sources")
        assert dep_on(_fake_request()) is None  # no raise

    def test_endpoint_shape(self, client):
        body = client.get("/api/system/capabilities").json()
        assert body["edition"] == "community"
        assert body["capabilities"]["watch_sources"] is True
        assert body["capabilities"]["billing"] is False
        assert body["audience"]["billing"] == "org_admin"
        assert body["audience"]["engine.settings"] == "platform"

    def test_every_capability_has_an_audience(self):
        from app.core.capabilities import CAPABILITY_AUDIENCE

        unclassified = set(COMMUNITY_CAPABILITIES) - set(CAPABILITY_AUDIENCE)
        assert not unclassified, f"capabilities missing audience: {unclassified}"
        valid = {"user", "team", "org_admin", "platform"}
        assert set(CAPABILITY_AUDIENCE.values()) <= valid

    def test_audience_separation_invariants(self):
        """Org admins manage their tenant, never the platform: no org_admin
        surface may be a platform config surface and vice versa."""
        from app.core.capabilities import CAPABILITY_AUDIENCE

        platform_keys = {k for k, a in CAPABILITY_AUDIENCE.items() if a == "platform"}
        org_keys = {k for k, a in CAPABILITY_AUDIENCE.items() if a == "org_admin"}
        assert not platform_keys & org_keys
        # The dangerous platform surfaces are classified as platform
        for key in ("auth.config_ui", "users.local_admin", "engine.settings"):
            assert CAPABILITY_AUDIENCE[key] == "platform"


class TestRouterGating:
    """Capability-gated routers: visible in community, 404 when a cloud-style
    resolver disables the surface, capabilities endpoint never gated."""

    def test_community_router_reachable(self, client):
        # Unauthenticated -> 401/403 (auth gate), NOT 404 (capability gate)
        assert client.get("/api/watch-sources").status_code != 404
        assert client.get("/api/llm-settings").status_code != 404

    def test_cloud_resolver_hides_gated_routers(self, client):
        set_capability_resolver(
            lambda _req: {
                "watch_sources": False,
                "asr.user_providers": False,
                "engine.settings": False,
                "llm.user_settings": False,
                "auth.config_ui": False,
            }
        )
        assert client.get("/api/watch-sources").status_code == 404
        assert client.get("/api/llm-settings").status_code == 404
        assert client.get("/api/asr-settings/providers").status_code == 404
        assert client.get("/api/admin/engine-settings").status_code == 404
        assert client.get("/api/admin/auth-config").status_code == 404
        # The capabilities endpoint itself must NEVER be gated
        assert client.get("/api/system/capabilities").status_code == 200


class TestPipelineHooks:
    def _dispatch_ctx(self, **overrides: Any) -> DispatchContext:
        defaults: dict[str, Any] = {
            "file_id": 1,
            "file_uuid": str(uuid.uuid4()),
            "user_id": 1,
            "organization_id": None,
            "est_audio_hours": None,
            "task_id": str(uuid.uuid4()),
        }
        defaults.update(overrides)
        return DispatchContext(**defaults)

    def test_no_hooks_is_noop(self):
        fire_before_dispatch(self._dispatch_ctx())  # no raise

    def test_quota_exceeded_propagates_as_402(self):
        def quota_hook(ctx: DispatchContext) -> None:
            raise QuotaExceededError(hours_over=1.5)

        register_before_dispatch(quota_hook)
        with pytest.raises(QuotaExceededError) as exc:
            fire_before_dispatch(self._dispatch_ctx())
        assert exc.value.status_code == 402
        assert exc.value.hours_over == 1.5

    def test_other_dispatch_hook_errors_contained(self):
        register_before_dispatch(lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
        fire_before_dispatch(self._dispatch_ctx())  # contained, no raise

    def test_completion_hook_receives_context_and_errors_contained(self):
        seen: list[CompletionContext] = []
        register_transcription_complete(seen.append)
        register_transcription_complete(
            lambda ctx: (_ for _ in ()).throw(RuntimeError("metering down"))
        )

        ctx = CompletionContext(
            file_id=7,
            file_uuid=str(uuid.uuid4()),
            user_id=3,
            organization_id=None,
            audio_duration_s=123.4,
            run_id="run-1",
            provider="local",
            success=True,
        )
        fire_transcription_complete(ctx)  # second hook crashing is contained
        assert seen == [ctx]


class TestRecordEvent:
    def test_records_and_dedupes(self, db_session):
        key = f"test:{uuid.uuid4().hex[:8]}"
        assert (
            record_event(
                db_session,
                event_type="transcription.hours",
                quantity=1.25,
                unit="hours",
                idempotency_key=key,
                metadata={"provider": "local"},
            )
            is True
        )
        # Replay with the same key is skipped, not an error
        assert (
            record_event(
                db_session,
                event_type="transcription.hours",
                quantity=1.25,
                unit="hours",
                idempotency_key=key,
            )
            is False
        )


class TestRequestContext:
    def _user(self, db_session, ident: Optional[ExternalIdentity] = None):
        return sync_external_user_to_db(db_session, ident or _identity())

    def test_personal_context_without_identity(self, db_session):
        user = self._user(db_session)
        ctx = get_current_context(_fake_request(), db=db_session, current_user=user)
        assert ctx.org_id is None
        assert not ctx.is_org_context

    def test_org_context_requires_membership_mirror(self, db_session):
        ident = _identity()
        user = self._user(db_session, ident)
        org = Organization(clerk_org_id=ident.org_id, name="Acme")
        db_session.add(org)
        db_session.commit()

        request = _fake_request(external_identity=ident)

        # Token claims the org but no membership row yet -> personal scope
        ctx = get_current_context(request, db=db_session, current_user=user)
        assert not ctx.is_org_context

        # Mirror confirms membership -> org scope + role
        db_session.add(
            OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:admin")
        )
        db_session.commit()
        ctx = get_current_context(request, db=db_session, current_user=user)
        assert ctx.org_id == org.id
        assert ctx.is_org_admin

    def test_scope_to_context_filters(self, db_session):
        ident = _identity()
        user = self._user(db_session, ident)
        org = Organization(clerk_org_id=ident.org_id, name="Acme")
        db_session.add(org)
        db_session.commit()

        mine_personal = MediaFile(
            user_id=user.id,
            filename="personal.mp3",
            storage_path=f"t/{uuid.uuid4().hex}.mp3",
            file_size=10,
            content_type="audio/mpeg",
        )
        org_file = MediaFile(
            user_id=user.id,
            organization_id=org.id,
            filename="org.mp3",
            storage_path=f"t/{uuid.uuid4().hex}.mp3",
            file_size=10,
            content_type="audio/mpeg",
        )
        db_session.add_all([mine_personal, org_file])
        db_session.commit()

        personal_ctx = RequestContext(user=user)
        org_ctx = RequestContext(user=user, org_id=org.id, org_role="org:member")

        personal_q = scope_to_context(db_session.query(MediaFile), MediaFile, personal_ctx)
        org_q = scope_to_context(db_session.query(MediaFile), MediaFile, org_ctx)

        personal_ids = {f.id for f in personal_q.all()}
        org_ids = {f.id for f in org_q.all()}

        assert mine_personal.id in personal_ids
        assert org_file.id in org_ids
        # A file uploaded into an org lives in the org space, NOT the
        # uploader's personal space — and vice versa (no cross-leak).
        assert org_file.id not in personal_ids
        assert mine_personal.id not in org_ids
