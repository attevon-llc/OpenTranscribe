"""Fixtures for the HTTP-level API suites under ``tests/api/``.

Two of them exist to make the **cloud tenancy seam reachable over HTTP** without a
cloud IdP. The org-scoped surfaces (org-admin, and the tenant gate on
``GET /users/search``) were previously only ever tested by calling the handler
function directly with a hand-built ``RequestContext`` — which proves the body's
logic and nothing about the dependency chain in front of it: not the capability
gate, not ``require_org_admin``, not the account-lifecycle gate, and not the
ordering between them.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from types import SimpleNamespace

import pytest


@pytest.fixture
def org_context(monkeypatch) -> Callable[..., None]:
    """Install a fake org resolution for the duration of one test.

    ``resolve_org_context`` is the ONLY step of ``get_current_context`` that needs
    a cloud identity provider (it reads ``request.state.external_identity``, which
    the community edition never sets). Replacing just that function keeps the whole
    real chain in front of it — bearer token, ``get_current_active_user``'s
    lifecycle gates, ``require_org_admin``, the router's capability dependency — so
    a test can exercise an org-admin request end to end over HTTP.

    Overriding ``get_current_context`` itself would have been shorter and wrong:
    it would remove the authentication and lifecycle gates from the very routes
    whose gating is under test.

    Usage::

        org_context(org_id=org.id, org_role="org:admin", only_for=admin_a.id)

    Args:
        Returned callable takes ``org_id``, ``org_role`` and an optional
        ``only_for`` user id. Any other user resolves to personal scope, so one
        test can drive both an org admin and an outsider.
    """

    def _install(org_id: int | None, org_role: str | None, only_for: int | None = None) -> None:
        def _fake_resolve(_request, _db, user):
            if only_for is not None and int(user.id) != int(only_for):
                return None, None
            return org_id, org_role

        monkeypatch.setattr("app.api.deps_context.resolve_org_context", _fake_resolve)

    return _install


@pytest.fixture
def organizations_capability_on():
    """Enable the cloud-only ``organizations`` capability for one test.

    Without this the ``/api/org-admin`` router answers 404 for everyone
    (``require_capability``), which is itself asserted by
    ``test_org_admin_endpoints.py`` — so every test that wants to reach a handler
    under that prefix has to turn the surface on first, exactly as the cloud
    resolver does.
    """
    from app.core.capabilities import COMMUNITY_CAPABILITIES
    from app.core.capabilities import reset_capability_resolver
    from app.core.capabilities import set_capability_resolver

    # The full map: a resolver's omitted keys are denied (#868), not defaulted.
    set_capability_resolver(lambda _request: {**COMMUNITY_CAPABILITIES, "organizations": True})
    yield
    reset_capability_resolver()


@pytest.fixture
def rate_limiting_enabled():
    """Turn the real slowapi limiter on for one test, with clean counters either side.

    ``tests/conftest.py`` builds the module-level ``limiter`` with ``enabled=False`` so no
    other test is rate-limited by accident; ``Limiter.enabled`` is a plain attribute, so it
    can be flipped per test. Counters are cleared on entry too, because a rate-limited
    request made by a test that did NOT use this fixture would otherwise leak in.
    """
    from app.auth.rate_limit import limiter
    from tests.helpers import reset_rate_limiter

    was_enabled = limiter.enabled
    limiter.enabled = True
    reset_rate_limiter()
    try:
        yield
    finally:
        limiter.enabled = was_enabled
        reset_rate_limiter()


@pytest.fixture
def pipeline_stages(monkeypatch, db_session):
    """Run the REAL dispatch and capture what the first pipeline stage is built with.

    ``preprocess_for_transcription`` is the stage whose keyword arguments carry the per-file
    options into the pipeline, so its signature is the value that "reaches the task". Only
    the broker publish (``chain(...).apply_async``) is stubbed.
    """

    import app.tasks.transcription.dispatch as dispatch_module

    @contextlib.contextmanager
    def _scope():
        yield db_session

    stages: list[dict] = []

    def _chain(preprocess, *rest):
        stages.append(dict(preprocess.kwargs))
        return SimpleNamespace(apply_async=lambda **kw: SimpleNamespace(id="stub"))

    monkeypatch.setenv("SKIP_CELERY", "False")
    monkeypatch.setattr(dispatch_module, "session_scope", _scope)
    monkeypatch.setattr(dispatch_module, "chain", _chain)
    return stages


@pytest.fixture
def pipeline_env(monkeypatch):
    """A full deployment whose users default to local Whisper and the native diarizer.

    Pins the ambient values the pipeline plan reads (``ASR_PROVIDER``, ``DEPLOYMENT_MODE``,
    ``engine.diarizer_backend``) so a developer's ``.env`` cannot change a cell. Returns a
    setter for the deployment mode.
    """
    from app.core.config import settings
    from app.transcription.config import TranscriptionConfig

    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "full")
    monkeypatch.setattr(
        TranscriptionConfig, "_resolve_diarizer_backend", staticmethod(lambda: "native")
    )

    def _mode(mode: str) -> None:
        monkeypatch.setattr(settings, "DEPLOYMENT_MODE", mode)

    return _mode


@pytest.fixture
def add_asr_config(db_session, normal_user) -> Callable[..., object]:
    """Store a ``UserASRSettings`` row; ``active=True`` also points the user at it.

    ``enabled`` is the row's own ``is_active`` flag (switched on/off), distinct from ``active``,
    which is whether the user's ``active_asr_config_id`` pointer names it.

    Writes the same row plus ``active_asr_config_id`` pointer the ASR settings endpoints
    write, so the code under test resolves it through its real path.
    """
    from app.models import UserSetting
    from app.models.user_asr_settings import UserASRSettings

    def _add(
        provider: str,
        model: str,
        *,
        active: bool = False,
        owner=None,
        shared=False,
        enabled: bool = True,
    ):
        owner = owner or normal_user
        cfg = UserASRSettings(
            user_id=owner.id,
            name=f"{provider}-{model}-fixture",
            provider=provider,
            model_name=model,
            is_active=enabled,
            is_shared=shared,
        )
        db_session.add(cfg)
        db_session.commit()
        db_session.refresh(cfg)
        if active:
            db_session.add(
                UserSetting(
                    user_id=owner.id,
                    setting_key="active_asr_config_id",
                    setting_value=str(cfg.id),
                )
            )
            db_session.commit()
        return cfg

    return _add


@pytest.fixture
def store_pyannote_key(db_session, normal_user) -> Callable[[], None]:
    """Give ``normal_user`` a stored (encrypted) pyannote.ai key, as the key endpoint does."""
    from app.models.user_diarization_settings import UserDiarizationSettings
    from app.utils.encryption import encrypt_api_key

    def _store() -> None:
        db_session.add(
            UserDiarizationSettings(
                user_id=normal_user.id,
                name="pyannote.ai",
                provider="pyannote",
                model_name="precision-3",
                api_key=encrypt_api_key("fixture-key-aaaa-1111"),
                is_active=True,
            )
        )
        db_session.commit()

    return _store


@pytest.fixture
def caps_off():
    """Turn capability keys off for the duration of a test."""
    from app.core.capabilities import COMMUNITY_CAPABILITIES
    from app.core.capabilities import reset_capability_resolver
    from app.core.capabilities import set_capability_resolver

    def _install(*keys: str) -> None:
        overrides = dict.fromkeys(keys, False)
        set_capability_resolver(lambda _request: {**COMMUNITY_CAPABILITIES, **overrides})

    yield _install
    reset_capability_resolver()
