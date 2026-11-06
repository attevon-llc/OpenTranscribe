"""Protected-media imports through the HTTP API, and the per-user sources the plugin reads.

Characterization tests pinned before the rewrite of the protected-media code:

* ``POST /api/files/process-url`` hands the request's site credentials to the synchronous
  metadata lookup by keyword, and to the worker only as an opaque reference; a playlist
  never carries them; a private address is refused before any provider is asked.
* ``GET /api/system/config/protected-media-auth`` error mapping.
* The MediaCMS plugin's per-user source rows and the caller-scoped public config, read from
  real database rows (the plugin opens its own ``SessionLocal``; it is pointed at the test
  transaction here).

Metadata extraction and the Celery publish are stubbed; validation, the placeholder row and
the credential hand-off are real.
"""

from __future__ import annotations

import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi import status

import app.api.endpoints.files.url_processing as url_processing
import app.api.endpoints.files.url_video_import as url_video_import
import app.api.endpoints.system as system_endpoints
import app.utils.task_secret_handoff as handoff
from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.user import User
from app.models.user_media_source import UserMediaSource
from app.services import media_download_service as mds
from app.services.media_download_service import MediaDownloadService
from app.services.protected_media_plugins.mediacms import MediacmsProvider
from app.utils.encryption import encrypt_api_key
from tests.helpers import media_file_count
from tests.helpers import stub_public_dns

MEDIA_URL = "https://m.example.com/view?m=abc"
SECRET = "hunter2-protected"
LOGIN = "alice-media-login"


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def setex(self, key, _ttl, value):
        self.store[key] = value

    def get(self, key):
        value = self.store.get(key)
        return value.encode() if isinstance(value, str) else value

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def seams(monkeypatch):
    """Record the metadata lookups and both dispatches; keep the hand-off in memory."""
    stub_public_dns(monkeypatch)
    monkeypatch.setattr(settings, "YOUTUBE_USER_RATE_LIMIT_ENABLED", False)
    redis = _FakeRedis()
    monkeypatch.setattr(handoff, "get_redis", lambda: redis)
    extracted: list[tuple[str, dict]] = []
    tasks: list[dict] = []
    playlists: list[dict] = []

    def _extract(self, url, **kwargs):
        extracted.append((url, kwargs))
        return {"id": f"prot-{uuid_pkg.uuid4().hex[:10]}", "title": "t", "extractor": "mediacms"}

    def _record(into: list[dict]):
        def _delay(**kwargs):
            into.append(kwargs)
            return SimpleNamespace(id="stub-task")

        return _delay

    monkeypatch.setattr(MediaDownloadService, "extract_video_info", _extract)
    monkeypatch.setattr(url_video_import.process_youtube_url_task, "delay", _record(tasks))
    monkeypatch.setattr(url_processing.process_youtube_playlist_task, "delay", _record(playlists))
    return SimpleNamespace(redis=redis, extracted=extracted, tasks=tasks, playlists=playlists)


def _post(client, headers, body: dict):
    return client.post("/api/files/process-url", headers=headers, json=body)


def test_credentials_reach_the_lookup_by_keyword_and_the_worker_only_by_reference(
    client, user_token_headers, normal_user, db_session, seams
):
    response = _post(
        client,
        user_token_headers,
        {"url": MEDIA_URL, "media_username": LOGIN, "media_password": SECRET},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert SECRET not in response.text and LOGIN not in response.text
    assert seams.extracted == [
        (
            MEDIA_URL,
            {"media_username": LOGIN, "media_password": SECRET, "user_id": normal_user.id},
        )
    ]
    assert len(seams.tasks) == 1
    task = seams.tasks[0]
    assert SECRET not in repr(task) and LOGIN not in repr(task)
    file_uuid = response.json()["uuid"]
    assert (task["url"], task["user_id"], task["file_uuid"]) == (
        MEDIA_URL,
        normal_user.id,
        file_uuid,
    )
    held = handoff.fetch_task_secret(
        task["media_credentials_ref"],
        context=handoff.media_credentials_context(normal_user.id, file_uuid),
    )
    assert held == {"media_username": LOGIN, "media_password": SECRET}
    row = db_session.query(MediaFile).filter(MediaFile.uuid == file_uuid).one()
    assert SECRET not in repr(row.metadata_raw)


def test_an_import_without_credentials_passes_no_reference(
    client, user_token_headers, normal_user, seams
):
    response = _post(client, user_token_headers, {"url": MEDIA_URL})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert seams.extracted == [
        (MEDIA_URL, {"media_username": None, "media_password": None, "user_id": normal_user.id})
    ]
    assert seams.tasks[0]["media_credentials_ref"] is None
    assert seams.redis.store == {}


def test_a_playlist_never_carries_credentials(client, user_token_headers, seams, monkeypatch):
    monkeypatch.setattr(MediaDownloadService, "is_playlist_url", lambda self, url: True)

    response = _post(
        client,
        user_token_headers,
        {
            "url": "https://www.youtube.com/playlist?list=PLprot",
            "media_username": LOGIN,
            "media_password": SECRET,
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert len(seams.playlists) == 1
    assert SECRET not in repr(seams.playlists) and LOGIN not in repr(seams.playlists)
    assert (seams.extracted, seams.tasks, seams.redis.store) == ([], [], {})


def test_a_private_protected_url_is_refused_before_any_provider_is_asked(
    client, user_token_headers, normal_user, db_session, seams, monkeypatch
):
    asked: list[str] = []

    class _ClaimsEverything:
        def can_handle(self, url, user_id=None):
            asked.append(url)
            return True

    monkeypatch.setattr(mds, "PROTECTED_MEDIA_PROVIDERS", [_ClaimsEverything()])
    before = media_file_count(db_session, normal_user.id)

    response = _post(
        client,
        user_token_headers,
        {"url": "http://10.0.0.5/view?m=abc", "media_username": LOGIN, "media_password": SECRET},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {"detail": "Invalid URL. Please enter a valid HTTP or HTTPS URL."}
    assert (asked, seams.extracted, seams.tasks, seams.redis.store) == ([], [], [], {})
    assert media_file_count(db_session, normal_user.id) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [("media_username", 5), ("media_password", ["x"])],
)
def test_credential_fields_are_optional_strings(client, user_token_headers, seams, field, value):
    response = _post(client, user_token_headers, {"url": MEDIA_URL, field: value})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert seams.extracted == []


def test_the_request_schema_lists_the_credential_fields_after_the_url():
    from app.api.endpoints.files.url_request import URLProcessingRequest

    fields = list(URLProcessingRequest.model_fields)
    assert fields[:3] == ["url", "media_username", "media_password"]
    for name in ("media_username", "media_password"):
        info = URLProcessingRequest.model_fields[name]
        assert (info.default, info.annotation, bool(info.description)) == (None, str | None, True)


# ---------------------------------------------------------------- GET protected-media-auth

_CONFIG_URL = "/api/system/config/protected-media-auth"


def test_the_config_route_resolves_for_the_caller(
    client, user_token_headers, normal_user, monkeypatch
):
    seen: list[int | None] = []

    def _config(user_id=None):
        seen.append(user_id)
        return [{"hosts": ["h.example.com"], "auth_type": "user_password", "fields": []}]

    monkeypatch.setattr(system_endpoints, "get_protected_media_auth_config", _config)
    response = client.get(_CONFIG_URL, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == [
        {"hosts": ["h.example.com"], "auth_type": "user_password", "fields": []}
    ]
    assert seen == [normal_user.id]


@pytest.mark.parametrize(
    ("raised", "code", "detail"),
    [
        (
            RuntimeError("plugin bug at 10.1.2.3"),
            500,
            "Error retrieving protected media configuration",
        ),
        (InvalidInputError("refused by the provider"), 400, "refused by the provider"),
        (HTTPException(status_code=418, detail="teapot"), 418, "teapot"),
    ],
)
def test_the_config_route_error_mapping(
    client, user_token_headers, monkeypatch, raised, code, detail
):
    def _config(user_id=None):
        raise raised

    monkeypatch.setattr(system_endpoints, "get_protected_media_auth_config", _config)
    response = client.get(_CONFIG_URL, headers=user_token_headers)

    assert response.status_code == code
    assert response.json() == {"detail": detail}


# ---------------------------------------------------------------- per-user source rows


class _Borrowed:
    """The test transaction's session, lent to code that opens and closes its own."""

    def __init__(self, session) -> None:
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        return None


@pytest.fixture
def plugin_db(monkeypatch, db_session):
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _Borrowed(db_session))
    monkeypatch.delenv("MEDIACMS_ALLOWED_HOSTS", raising=False)
    return db_session


def _user(db, label: str) -> User:
    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=label,
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _source(db, owner: User, host: str, **kw: Any) -> str:
    db.add(
        UserMediaSource(
            user_id=owner.id, hostname=host, provider_type=kw.pop("provider_type", "mediacms"), **kw
        )
    )
    db.commit()
    return host


def _host(tag: str) -> str:
    return f"{tag}-{uuid_pkg.uuid4().hex[:8]}.example.com"


def test_per_user_rows_are_mapped_with_the_password_decrypted(plugin_db):
    db = plugin_db
    owner, sharer = _user(db, "owner"), _user(db, "sharer")
    full = _source(
        db,
        owner,
        _host("full"),
        username="u1",
        password=encrypt_api_key("p1"),
        verify_ssl=False,
        label="Mine",
    )
    bare = _source(db, owner, _host("bare"))
    broken = _source(db, owner, _host("broken"), username="u3", password="not-a-ciphertext")
    shared = _source(
        db, sharer, _host("shared"), username="s", password=encrypt_api_key("sp"), is_shared=True
    )
    _source(db, sharer, _host("private"), username="x", password=encrypt_api_key("x"))
    _source(db, owner, _host("inactive"), is_active=False)
    _source(db, owner, _host("other"), provider_type="other")

    rows = MediacmsProvider()._get_user_media_sources(owner.id)

    by_host = {r["hostname"]: r for r in rows}
    assert set(by_host) == {full, bare, broken, shared}
    assert [r["hostname"] for r in rows][-1] == shared, "own rows come first"
    assert by_host[full] == {
        "hostname": full,
        "provider_type": "mediacms",
        "username": "u1",
        "password": "p1",
        "verify_ssl": False,
        "label": "Mine",
        "user_id": owner.id,
    }
    assert (by_host[bare]["username"], by_host[bare]["password"], by_host[bare]["label"]) == (
        "",
        "",
        "",
    )
    assert by_host[bare]["verify_ssl"] is True
    # One undecryptable password blanks only that source (see defect D-12 in the register).
    assert (by_host[broken]["username"], by_host[broken]["password"]) == ("u3", "")
    assert (by_host[shared]["password"], by_host[shared]["user_id"]) == ("sp", sharer.id)


def test_no_user_means_no_per_user_rows(plugin_db):
    owner = _user(plugin_db, "owner")
    _source(
        plugin_db, owner, _host("x"), username="u", password=encrypt_api_key("p"), is_shared=True
    )
    assert MediacmsProvider()._get_user_media_sources(None) == []


def test_public_config_follows_the_callers_tenant(plugin_db, monkeypatch):
    db = plugin_db
    org = Organization(
        external_org_id=f"org_{uuid_pkg.uuid4().hex[:8]}", name="Prot Org", is_active=True
    )
    db.add(org)
    db.commit()
    personal_sharer, personal_peer, member = _user(db, "ps"), _user(db, "pp"), _user(db, "m")
    db.add(OrganizationMembership(organization_id=org.id, user_id=member.id, role="org:member"))
    db.commit()
    shared = _source(
        db,
        personal_sharer,
        _host("shared"),
        username="svc",
        password=encrypt_api_key("pw"),
        is_shared=True,
    )
    provider = MediacmsProvider()
    monkeypatch.setattr(provider, "_get_db_sources", list)

    peer = provider.get_public_auth_config(user_id=personal_peer.id)
    other_tenant = provider.get_public_auth_config(user_id=member.id)

    assert peer["hosts"] == [shared]
    assert peer["hosts_with_stored_credentials"] == [shared]
    assert other_tenant == {}
