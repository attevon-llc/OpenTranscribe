"""Cross-tenant regression suite for every per-user item that can be shared.

One world, every shared resource type, the same three questions each time:

* **list** — does the item appear in the viewer's "shared with me" listing?
* **use** — can the viewer read/select it through the API?
* **worker** — does a background task honour a stored pointer to it?

The world: organization A (``a1`` shares, ``a2``), organization B (``b1``) and two
accounts with no organization (``p1`` shares, ``p2``). The rule under test is the one
``groups._same_tenant`` and ``GET /users/search`` apply: in an organization, a shared
item reaches the viewer only if its owner is a member of that organization; in personal
scope, only if the owner belongs to no organization. Community installs have no
organizations, so every account is "personal" and instance-wide sharing is unchanged —
that is the ``p1`` → ``p2`` row.

Adding a shared resource type? Add a ``Surface`` to ``SURFACES``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.prompt import SummaryPrompt
from app.models.prompt import UserSetting
from app.models.user import User
from app.models.user_asr_settings import UserASRSettings
from app.models.user_llm_settings import UserLLMSettings
from app.models.user_media_source import UserMediaSource

# --------------------------------------------------------------------------- #
# World                                                                        #
# --------------------------------------------------------------------------- #


def _mk_user(db, label: str) -> User:
    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=f"{label} user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_org(db, label: str) -> Organization:
    org = Organization(
        external_org_id=f"org_{label}_{uuid_pkg.uuid4().hex[:8]}",
        name=f"{label} Org",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _join(db, org: Organization, user: User) -> None:
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:member"))
    db.commit()


@dataclass
class World:
    db: Any
    org_a: Organization
    org_b: Organization
    users: dict[str, User]

    def org_id(self, name: str | None) -> int | None:
        return {"A": self.org_a.id, "B": self.org_b.id, None: None}[name]


@pytest.fixture()
def world(db_session) -> World:
    db = db_session
    org_a, org_b = _mk_org(db, "A"), _mk_org(db, "B")
    users = {name: _mk_user(db, name) for name in ("a1", "a2", "b1", "p1", "p2")}
    _join(db, org_a, users["a1"])
    _join(db, org_a, users["a2"])
    _join(db, org_b, users["b1"])
    return World(db=db, org_a=org_a, org_b=org_b, users=users)


@contextmanager
def _acting_as(user: User, org_id: int | None):
    """Run requests as ``user`` in tenant ``org_id`` (None = personal workspace)."""
    from app.api.deps_context import RequestContext
    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user
    from app.main import app

    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user=user, org_id=org_id, org_role="org:member" if org_id is not None else None
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_current_context, None)


def _set_setting(db, user: User, key: str, value: str) -> None:
    row = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.setting_key == key)
        .first()
    )
    if row is None:
        db.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    else:
        row.setting_value = value
    db.commit()


# --------------------------------------------------------------------------- #
# Surfaces                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Surface:
    """One shareable resource type and the three ways another user could reach it.

    ``share(db, owner)`` creates a shared item and returns a handle; ``listed`` returns
    the handles in the viewer's shared listing; ``use`` returns True when the API lets
    the viewer read/select the item; ``worker`` returns True when a background path
    honours the viewer's stored pointer to it (None = no such path).
    """

    name: str
    models: tuple[type, ...]
    share: Callable[[Any, User], Any]
    listed: Callable[[Any], set]
    use: Callable[[Any, Any], bool] | None
    worker: Callable[[Any, User, Any], bool] | None


# --- LLM configurations (carry the owner's provider API key) ---------------- #


def _share_llm(db, owner: User) -> str:
    cfg = UserLLMSettings(
        user_id=owner.id,
        name=f"shared-llm-{uuid_pkg.uuid4().hex[:6]}",
        provider="openai",
        model_name="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        max_tokens=8192,
        temperature="0.3",
        is_shared=True,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return str(cfg.uuid)


def _llm_listed(client) -> set:
    resp = client.get("/api/llm-settings")
    assert resp.status_code == 200, resp.text
    return {str(c["uuid"]) for c in resp.json()["shared_configurations"]}


def _llm_use(client, cfg_uuid: str) -> bool:
    read = client.get(f"/api/llm-settings/config/{cfg_uuid}")
    select = client.post("/api/llm-settings/set-active", json={"configuration_id": cfg_uuid})
    assert read.status_code in (200, 403), read.text
    assert select.status_code in (200, 403), select.text
    assert (read.status_code == 200) == (select.status_code == 200)
    return bool(select.status_code == 200)


def _llm_worker(db, user: User, cfg_uuid: str) -> bool:
    from app.services.llm_service import LLMService

    cfg = db.query(UserLLMSettings).filter(UserLLMSettings.uuid == cfg_uuid).one()
    _set_setting(db, user, "active_llm_config_id", str(cfg.id))
    resolved = LLMService._resolve_user_llm_settings(db, user.id)
    return resolved is not None and resolved.settings.id == cfg.id


# --- ASR configurations (carry the owner's provider API key) ---------------- #


def _share_asr(db, owner: User) -> str:
    cfg = UserASRSettings(
        user_id=owner.id,
        name=f"shared-asr-{uuid_pkg.uuid4().hex[:6]}",
        provider="deepgram",
        model_name=f"nova-{uuid_pkg.uuid4().hex[:6]}",
        is_shared=True,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return str(cfg.uuid)


def _asr_listed(client) -> set:
    resp = client.get("/api/asr-settings")
    assert resp.status_code == 200, resp.text
    return {str(c["uuid"]) for c in resp.json()["shared_configs"]}


def _asr_use(client, cfg_uuid: str) -> bool:
    read = client.get(f"/api/asr-settings/config/{cfg_uuid}")
    select = client.post("/api/asr-settings/set-active", json={"config_uuid": cfg_uuid})
    assert read.status_code in (200, 404), read.text
    assert select.status_code in (200, 404), select.text
    assert (read.status_code == 200) == (select.status_code == 200)
    return bool(select.status_code == 200)


def _asr_worker(db, user: User, cfg_uuid: str) -> bool:
    from app.services.asr.factory import ASRProviderFactory

    cfg = db.query(UserASRSettings).filter(UserASRSettings.uuid == cfg_uuid).one()
    _set_setting(db, user, "active_asr_config_id", str(cfg.id))
    caps = ASRProviderFactory.get_active_model_capabilities(user.id, db)
    return bool(caps.get("model_id") == cfg.model_name)


# --- Media sources (carry the owner's stored credentials) ------------------- #


def _share_media_source(db, owner: User) -> str:
    hostname = f"media-{uuid_pkg.uuid4().hex[:8]}.example.com"
    db.add(
        UserMediaSource(
            user_id=owner.id,
            hostname=hostname,
            provider_type="mediacms",
            username="svc",
            is_active=True,
            is_shared=True,
        )
    )
    db.commit()
    return hostname


def _media_listed(client) -> set:
    resp = client.get("/api/user-settings/media-sources")
    assert resp.status_code == 200, resp.text
    return {s["hostname"] for s in resp.json()["shared_sources"]}


def _media_worker(db, user: User, hostname: str) -> bool:
    from app.services.protected_media_plugins.mediacms import MediacmsProvider

    return hostname in {s.hostname for s in MediacmsProvider._query_user_media_sources(db, user.id)}


# --- Organization context (UserSetting-backed, no model of its own) --------- #


def _share_org_context(db, owner: User) -> str:
    _set_setting(db, owner, "org_context_text", f"context of {owner.email}")
    _set_setting(db, owner, "org_context_is_shared", "true")
    return str(owner.id)


def _org_context_listed(client) -> set:
    resp = client.get("/api/user-settings/organization-context/shared")
    assert resp.status_code == 200, resp.text
    return {c["user_id"] for c in resp.json()["shared_contexts"]}


def _org_context_use(client, owner_id: str) -> bool:
    resp = client.post(
        "/api/user-settings/organization-context/use-shared", json={"user_id": owner_id}
    )
    assert resp.status_code in (200, 404), resp.text
    return bool(resp.status_code == 200)


def _org_context_worker(db, user: User, owner_id: str) -> bool:
    from unittest.mock import patch

    from app.tasks.summarization import _get_organization_context

    _set_setting(db, user, "org_context_use_shared_from", owner_id)
    owner = db.get(User, int(owner_id))
    with patch("app.utils.prompt_manager.get_user_active_prompt_info", return_value=("p", True)):
        return _get_organization_context(db, user.id) == f"context of {owner.email}"


# --- Summary prompts (org-stamped where the edition stamps them) ------------ #


def _share_prompt(db, owner: User, org_id: int | None = None, tag: str = "t") -> str:
    prompt = SummaryPrompt(
        user_id=owner.id,
        organization_id=org_id,
        name=f"shared-prompt-{uuid_pkg.uuid4().hex[:6]}",
        prompt_text="Summarize {transcript}",
        content_type="general",
        is_system_default=False,
        is_active=True,
        is_shared=True,
        tags=[tag],
    )
    db.add(prompt)
    db.commit()
    db.refresh(prompt)
    return str(prompt.uuid)


def _prompt_listed(client) -> set:
    listing = client.get(
        "/api/prompts", params={"include_system": "false", "include_user": "false"}
    )
    by_type = client.get("/api/prompts/by-content-type/general")
    assert listing.status_code == 200, listing.text
    assert by_type.status_code == 200, by_type.text
    a = {str(p["uuid"]) for p in listing.json()["prompts"]}
    b = {str(p["uuid"]) for p in by_type.json()["shared_prompts"]}
    assert a == b
    return a


def _prompt_use(client, prompt_uuid: str) -> bool:
    read = client.get(f"/api/prompts/{prompt_uuid}")
    select = client.post("/api/prompts/active/set", json={"prompt_id": prompt_uuid})
    clone = client.post(f"/api/prompts/{prompt_uuid}/clone")
    codes = {read.status_code, select.status_code, clone.status_code}
    assert codes <= {200, 403}, (read.text, select.text, clone.text)
    assert len(codes) == 1, (read.status_code, select.status_code, clone.status_code)
    return bool(read.status_code == 200)


def _prompt_worker(db, user: User, prompt_uuid: str) -> bool:
    from app.utils.prompt_manager import resolve_active_prompt_record

    prompt = db.query(SummaryPrompt).filter(SummaryPrompt.uuid == prompt_uuid).one()
    _set_setting(db, user, "active_summary_prompt_id", str(prompt.id))
    resolved = resolve_active_prompt_record(user.id, db)
    return resolved is not None and resolved.id == prompt.id


SURFACES: list[Surface] = [
    Surface("llm_config", (UserLLMSettings,), _share_llm, _llm_listed, _llm_use, _llm_worker),
    Surface("asr_config", (UserASRSettings,), _share_asr, _asr_listed, _asr_use, _asr_worker),
    Surface(
        "media_source", (UserMediaSource,), _share_media_source, _media_listed, None, _media_worker
    ),
    Surface(
        "org_context",
        (),
        _share_org_context,
        _org_context_listed,
        _org_context_use,
        _org_context_worker,
    ),
    Surface(
        "summary_prompt",
        (SummaryPrompt,),
        _share_prompt,
        _prompt_listed,
        _prompt_use,
        _prompt_worker,
    ),
]


# --------------------------------------------------------------------------- #
# The matrix                                                                   #
# --------------------------------------------------------------------------- #

#: (viewer, viewer's active tenant, owner, expected). Owners: a1 (org A), p1 (no org).
CASES = [
    pytest.param("a2", "A", "a1", True, id="same-org"),
    pytest.param("b1", "B", "a1", False, id="other-org"),
    pytest.param("b1", "B", "p1", False, id="org-member-vs-personal-owner"),
    pytest.param("p2", None, "a1", False, id="personal-vs-org-owner"),
    pytest.param("p2", None, "p1", True, id="personal-to-personal"),
]

#: Worker paths have no active tenant: they ask whether owner and viewer share one.
WORKER_CASES = [
    pytest.param("a2", "a1", True, id="same-org"),
    pytest.param("b1", "a1", False, id="other-org"),
    pytest.param("b1", "p1", False, id="org-member-vs-personal-owner"),
    pytest.param("p2", "a1", False, id="personal-vs-org-owner"),
    pytest.param("p2", "p1", True, id="personal-to-personal"),
]


def _surface_params(attr: str) -> list:
    return [pytest.param(s, id=s.name) for s in SURFACES if getattr(s, attr) is not None]


@pytest.mark.parametrize("surface", _surface_params("listed"))
@pytest.mark.parametrize(("viewer", "tenant", "owner", "expected"), CASES)
def test_shared_listing(client, world, surface, viewer, tenant, owner, expected):
    handle = surface.share(world.db, world.users[owner])
    with _acting_as(world.users[viewer], world.org_id(tenant)):
        assert (handle in surface.listed(client)) is expected


@pytest.mark.parametrize("surface", _surface_params("use"))
@pytest.mark.parametrize(("viewer", "tenant", "owner", "expected"), CASES)
def test_shared_use(client, world, surface, viewer, tenant, owner, expected):
    handle = surface.share(world.db, world.users[owner])
    with _acting_as(world.users[viewer], world.org_id(tenant)):
        assert surface.use(client, handle) is expected


@pytest.mark.parametrize("surface", _surface_params("worker"))
@pytest.mark.parametrize(("viewer", "owner", "expected"), WORKER_CASES)
def test_shared_worker_pointer(world, surface, viewer, owner, expected):
    """A stored pointer (possibly written before this rule existed) is re-checked."""
    handle = surface.share(world.db, world.users[owner])
    assert surface.worker(world.db, world.users[viewer], handle) is expected


@pytest.mark.parametrize("surface", _surface_params("listed"))
def test_owner_never_lists_own_item_as_shared(client, world, surface):
    handle = surface.share(world.db, world.users["a1"])
    with _acting_as(world.users["a1"], world.org_a.id):
        assert handle not in surface.listed(client)


# --------------------------------------------------------------------------- #
# Key-bearing endpoints that dial a caller-chosen URL                          #
# --------------------------------------------------------------------------- #


def test_llm_test_connection_never_lends_a_shared_configs_key(client, world, monkeypatch):
    """``POST /llm-settings/test`` takes the caller's ``base_url``; a shared config's
    stored key must not ride along, even inside the owner's organization."""
    from app.api.endpoints import llm_settings
    from app.utils.encryption import encrypt_api_key

    cfg_uuid = _share_llm(world.db, world.users["a1"])
    cfg = world.db.query(UserLLMSettings).filter(UserLLMSettings.uuid == cfg_uuid).one()
    cfg.api_key = encrypt_api_key("sk-owner-secret")  # gitleaks:allow - fixture value
    world.db.commit()

    seen: list[str | None] = []

    class _Svc:
        def __init__(self, config):
            seen.append(config.api_key)
            self.endpoints = {config.provider: config.base_url}

        def validate_connection(self):
            return True, "ok"

        def close(self):
            pass

    monkeypatch.setattr(llm_settings, "LLMService", _Svc)
    monkeypatch.setattr(llm_settings, "_assert_safe_llm_endpoint", lambda *a, **k: None)
    body = {
        "provider": "openai",
        "model_name": "gpt-4o-mini",
        "base_url": "https://collector.example.net/v1",
        "config_id": cfg_uuid,
    }
    with _acting_as(world.users["a2"], world.org_a.id):
        assert client.post("/api/llm-settings/test", json=body).status_code == 200
    with _acting_as(world.users["a1"], world.org_a.id):
        assert client.post("/api/llm-settings/test", json=body).status_code == 200
    assert seen == [None, "sk-owner-secret"]


def test_model_discovery_never_lends_a_shared_configs_key(world):
    from app.api.endpoints.llm_settings import _get_stored_api_key
    from app.utils.encryption import encrypt_api_key

    cfg_uuid = _share_llm(world.db, world.users["a1"])
    cfg = world.db.query(UserLLMSettings).filter(UserLLMSettings.uuid == cfg_uuid).one()
    cfg.api_key = encrypt_api_key("sk-owner-secret")  # gitleaks:allow - fixture value
    world.db.commit()

    assert _get_stored_api_key(world.db, cfg_uuid, world.users["a2"].id) is None
    assert _get_stored_api_key(world.db, cfg_uuid, world.users["a1"].id) == "sk-owner-secret"


# --------------------------------------------------------------------------- #
# Chat: per-conversation model override and its message-time resolution        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("viewer", "tenant", "owner", "expected"), CASES)
def test_chat_llm_override_scoped(world, viewer, tenant, owner, expected):
    from fastapi import HTTPException

    from app.api.deps_context import RequestContext
    from app.api.endpoints.chat.common import resolve_llm_config_id

    cfg_uuid = _share_llm(world.db, world.users[owner])
    ctx = RequestContext(user=world.users[viewer], org_id=world.org_id(tenant))
    if expected:
        assert resolve_llm_config_id(world.db, ctx, cfg_uuid) is not None
    else:
        with pytest.raises(HTTPException) as exc:
            resolve_llm_config_id(world.db, ctx, cfg_uuid)
        assert exc.value.status_code == 404


@pytest.mark.parametrize(("viewer", "owner", "expected"), WORKER_CASES)
def test_chat_pinned_config_rechecked_at_message_time(world, monkeypatch, viewer, owner, expected):
    """``create_from_config_id`` runs on every chat turn against a stored config id."""
    from app.db import base as db_base
    from app.services.llm_service import LLMService

    class _Borrowed:
        def __init__(self, session):
            self._s = session

        def __getattr__(self, name):
            return getattr(self._s, name)

        def close(self):
            pass

    monkeypatch.setattr(db_base, "SessionLocal", lambda: _Borrowed(world.db))
    cfg_uuid = _share_llm(world.db, world.users[owner])
    cfg = world.db.query(UserLLMSettings).filter(UserLLMSettings.uuid == cfg_uuid).one()
    svc = LLMService.create_from_config_id(world.users[viewer].id, cfg.id)
    try:
        assert (svc is not None) is expected
    finally:
        if svc is not None:
            svc.close()


# --------------------------------------------------------------------------- #
# ASR transcription-time provider construction                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("viewer", "owner", "expected"), WORKER_CASES)
def test_asr_provider_for_transcription_scoped(world, monkeypatch, viewer, owner, expected):
    from app.services.asr import factory

    from_config, local_default = object(), object()
    monkeypatch.setenv("ASR_PROVIDER", "local")
    monkeypatch.setattr(factory, "_guard_local_allowed", lambda: None)
    monkeypatch.setattr(factory, "LocalASRProvider", lambda: local_default)
    monkeypatch.setattr(
        factory.ASRProviderFactory, "create_from_db_config", staticmethod(lambda cfg: from_config)
    )
    cfg_uuid = _share_asr(world.db, world.users[owner])
    cfg = world.db.query(UserASRSettings).filter(UserASRSettings.uuid == cfg_uuid).one()
    _set_setting(world.db, world.users[viewer], "active_asr_config_id", str(cfg.id))

    provider = factory.ASRProviderFactory.create_for_user(world.users[viewer].id, world.db)
    assert provider is (from_config if expected else local_default)


# --------------------------------------------------------------------------- #
# Summary prompts: library, tag list, org stamp, explicit prompt on a run       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("viewer", "tenant", "owner", "expected"), CASES)
def test_shared_prompt_library_scoped(client, world, viewer, tenant, owner, expected):
    tag = f"tag-{uuid_pkg.uuid4().hex[:8]}"
    prompt_uuid = _share_prompt(world.db, world.users[owner], tag=tag)
    with _acting_as(world.users[viewer], world.org_id(tenant)):
        resp = client.get("/api/prompts/shared/library")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (prompt_uuid in {str(p["uuid"]) for p in body["prompts"]}) is expected
    assert (tag in body["available_tags"]) is expected


def test_prompt_stamped_for_another_org_stays_there(client, world):
    """An owner in two organizations shares a prompt stamped for B: A never sees it."""
    _join(world.db, world.org_b, world.users["a1"])
    prompt_uuid = _share_prompt(world.db, world.users["a1"], org_id=world.org_b.id)
    with _acting_as(world.users["a2"], world.org_a.id):
        assert prompt_uuid not in _prompt_listed(client)
        assert _prompt_use(client, prompt_uuid) is False
    with _acting_as(world.users["b1"], world.org_b.id):
        assert prompt_uuid in _prompt_listed(client)
    assert _prompt_worker(world.db, world.users["a2"], prompt_uuid) is False
    assert _prompt_worker(world.db, world.users["b1"], prompt_uuid) is True


@pytest.mark.parametrize(("viewer", "tenant", "owner", "expected"), CASES)
def test_explicit_prompt_on_summarize_scoped(world, viewer, tenant, owner, expected):
    from fastapi import HTTPException

    from app.api.deps_context import RequestContext
    from app.api.endpoints.prompts import require_usable_prompt_uuid

    prompt_uuid = _share_prompt(world.db, world.users[owner])
    ctx = RequestContext(user=world.users[viewer], org_id=world.org_id(tenant))
    if expected:
        require_usable_prompt_uuid(world.db, prompt_uuid, ctx)
    else:
        with pytest.raises(HTTPException) as exc:
            require_usable_prompt_uuid(world.db, prompt_uuid, ctx)
        assert exc.value.status_code == 404
