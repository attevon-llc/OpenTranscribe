"""The authenticated backend API session used to delete candidates."""

from __future__ import annotations

import contextlib
import time
from typing import Any

import requests

from cleanup_test_data.settings import _admin_email, _admin_password

# ---------------------------------------------------------------------------------------------
# API deletion plane
# ---------------------------------------------------------------------------------------------

_DELETE_PATH_BY_RESOURCE = {
    'media_file': '/api/files/{uuid}',
    'collection': '/api/collections/{uuid}',
    'tag': None,  # bulk endpoint, handled separately (query-param list)
    'watch_source': '/api/watch-sources/{uuid}',
    'speaker_profile': '/api/speaker-profiles/profiles/{uuid}',
    'chat_conversation': '/api/chat/conversations/{uuid}',
    'speaker_cluster': '/api/speaker-clusters/{uuid}',
}


class ApiSession:
    """A thin, best-effort wrapper around the admin bearer token + CSRF cookie.

    Degrades rather than raises: if login fails, ``session`` is ``None`` and every
    caller checks that before attempting a delete — the DB-only planes (LLM configs,
    users, via ``cleanup-test-users.py``) still run.
    """

    def __init__(self, backend_url: str) -> None:
        self.backend_url = backend_url
        self.session: requests.Session | None = None

    def login(self) -> bool:
        session = requests.Session()
        try:
            resp = session.post(
                f'{self.backend_url}/api/auth/token',
                data={'username': _admin_email(), 'password': _admin_password()},
                timeout=30,
            )
        except requests.RequestException as exc:
            print(f'  WARN  could not reach backend at {self.backend_url}: {exc}')
            return False
        if resp.status_code != 200:
            print(f'  WARN  admin login failed: HTTP {resp.status_code}')
            return False
        csrf = session.cookies.get('csrf_token')
        if csrf:
            session.headers['X-CSRF-Token'] = csrf
        self.session = session
        return True

    def logout(self) -> None:
        """Courtesy fix (Decision 6): this tool must not itself contribute to the
        refresh_token leak it is adjacent to."""
        if self.session is None:
            return
        with contextlib.suppress(requests.RequestException):
            self.session.post(f'{self.backend_url}/api/auth/logout', timeout=15)

    def delete_user(self, user_uuid: str, email: str) -> bool:
        """``DELETE /api/admin/users/{uuid}``: the account and everything it owns (cascade +
        storage purge). The only way to remove a row the admin is not authorised on."""
        assert self.session is not None
        url = f'{self.backend_url}/api/admin/users/{user_uuid}'
        try:
            resp = self.session.delete(url, timeout=120)
        except requests.RequestException as exc:
            print(f'  FAILED  DELETE user {email} ({user_uuid}): {exc}')
            return False
        if resp.status_code in (200, 204, 404):
            return True
        print(f'  FAILED  DELETE user {email} ({user_uuid}): {describe_failure(resp)}')
        return False

    def delete_media_file(self, file_uuid: str) -> bool:
        """Mirrors ``e2e/conftest.py``'s ``delete_media_file``: wait out an
        ``active_task_id``, DELETE, fall back to ``/force`` on persistent 409."""
        assert self.session is not None
        last = 'no response'
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                detail = self.session.get(f'{self.backend_url}/api/files/{file_uuid}', timeout=30)
                if detail.status_code == 200 and detail.json().get('active_task_id') is not None:
                    time.sleep(3)
                    continue
            except requests.RequestException:
                pass
            break
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                resp = self.session.delete(f'{self.backend_url}/api/files/{file_uuid}', timeout=30)
                if resp.status_code in (200, 204, 404):
                    return self._purge_from_trash(file_uuid)
                last = describe_failure(resp)
            except requests.RequestException as exc:
                last = str(exc)
            time.sleep(5)
        try:
            resp = self.session.delete(
                f'{self.backend_url}/api/files/{file_uuid}/force', timeout=30
            )
            if resp.status_code in (200, 204, 404):
                return self._purge_from_trash(file_uuid)
            last = describe_failure(resp)
        except requests.RequestException as exc:
            last = str(exc)
        print(f'  FAILED  DELETE media_file {file_uuid}: {last}')
        return False

    def _purge_from_trash(self, file_uuid: str) -> bool:
        """A delete only trashes since #1101: destroy it (202), or it was already gone (404)."""
        assert self.session is not None
        try:
            resp = self.session.delete(
                f'{self.backend_url}/api/files/{file_uuid}/purge', timeout=30
            )
        except requests.RequestException as exc:
            print(f'  FAILED  purge media_file {file_uuid}: {exc}')
            return False
        if resp.status_code not in (202, 404):
            print(f'  FAILED  purge media_file {file_uuid}: {describe_failure(resp)}')
        return resp.status_code in (202, 404)

    def delete_simple(self, resource: str, resource_uuid: str) -> bool:
        assert self.session is not None
        path_template = _DELETE_PATH_BY_RESOURCE[resource]
        assert path_template is not None, f'{resource} has no simple delete path'
        url = f'{self.backend_url}{path_template.format(uuid=resource_uuid)}'
        try:
            resp = self.session.delete(url, timeout=30)
        except requests.RequestException as exc:
            print(f'  FAILED  DELETE {resource} {resource_uuid}: {exc}')
            return False
        if resp.status_code in (200, 204, 404):
            return True
        print(f'  FAILED  DELETE {resource} {resource_uuid}: {describe_failure(resp)}')
        return False

    def delete_tags(self, tag_uuids: list[str]) -> bool:
        assert self.session is not None
        try:
            resp = self.session.delete(
                f'{self.backend_url}/api/tags',
                params=[('tag_uuids', u) for u in tag_uuids],
                timeout=30,
            )
        except requests.RequestException as exc:
            print(f'  FAILED  DELETE {len(tag_uuids)} tag(s): {exc}')
            return False
        if not resp.ok:
            print(f'  FAILED  DELETE {len(tag_uuids)} tag(s): {describe_failure(resp)}')
        return bool(resp.ok)


def describe_failure(resp: Any) -> str:
    """``HTTP <status> <reason>: <detail>`` for a failed delete, bounded in length.

    The backend's ``detail`` is the reason an operator needs ("Not authorized to access this
    profile"); a non-JSON body falls back to its first 200 characters.
    """
    reason = getattr(resp, 'reason', '') or ''
    head = f'HTTP {resp.status_code}' + (f' {reason}' if reason else '')
    try:
        body = resp.json()
        detail = body.get('detail') if isinstance(body, dict) else None
    except ValueError:
        detail = None
    if detail is None:
        detail = (getattr(resp, 'text', '') or '').strip()[:200]
    return f'{head}: {str(detail)[:200]}' if detail else head
