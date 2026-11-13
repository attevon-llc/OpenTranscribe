"""The signed-in person every persona builds on: login, labelled calls, ids read from listings.

``FastHttpUser`` rather than ``HttpUser``: it drives the same requests with a fraction of the
load generator's CPU, which is what limits a single Locust process at hundreds of users (Locust
docs, "Increase performance with a faster HTTP client"). Its cookie jar carries the SPA's
session and CSRF cookies exactly as a browser would.
"""

from __future__ import annotations

import itertools
import random
import time
import uuid
from collections import defaultdict
from typing import Any

import routes
import scenario
from locust import FastHttpUser, between

_COUNTERS: defaultdict[str, itertools.count[int]] = defaultdict(itertools.count)
#: Files each signed-in identity has listed, shared by every simulated user in this process; the
#: cross-tenant canary asks for a file that belongs to somebody else.
FILES_BY_OWNER: defaultdict[str, list[str]] = defaultdict(list)


def _items(resp: Any, key: str | None = None) -> list[dict[str, Any]]:
    """The list in a JSON response (``resp.json()[key]`` or the body itself), else ``[]``."""
    if resp.status_code != 200:
        return []
    try:
        body = resp.json()
    except ValueError:
        return []
    found = body.get(key) if key and isinstance(body, dict) else body
    return [row for row in found if isinstance(row, dict)] if isinstance(found, list) else []


class SeededUser(FastHttpUser):
    """One signed-in person. Subclasses set ``pool`` (or override ``identity_index``)."""

    abstract = True
    pool = 'viewer'
    wait_time = between(3, 10)  # think time between page actions
    #: Listing parameters for the files this persona works on.
    file_filter: dict[str, Any] = {}

    def identity_index(self) -> int:
        return scenario.pool_index(self.pool, next(_COUNTERS[self.pool]), self.seeded_users)

    def on_start(self) -> None:
        options = self.environment.parsed_options
        self.timeout = options.request_timeout if options else 60.0
        self.seeded_users = options.seeded_users if options else 1000
        self.email = scenario.identity(self.identity_index())
        self.file_uuids: list[str] = []
        self._call('session', label='anonymous probe')
        self._login()
        self._call('system.capabilities')
        self._remember_files(
            self._call('files.list', params={'page': 1, 'page_size': 20, **self.file_filter})
        )

    def _login(self) -> None:
        self.client.cookiejar.clear()
        self._call(
            'login',
            data={'username': self.email, 'password': scenario.PASSWORD},
            relogin_on_401=False,
        )
        self._call('me')

    def _call(
        self,
        route: str,
        *,
        fill: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        label: str = '',
        expect: tuple[int, ...] = (200,),
        relogin_on_401: bool = True,
    ) -> Any:
        """One request through ``routes.ROUTES``, its failures labelled by cause."""
        method, template = routes.ROUTES[route]
        sent = dict(headers or {})
        if method not in ('GET', 'HEAD'):
            sent['X-CSRF-Token'] = scenario.cookie_value(self.client.cookiejar, 'csrf_token')
        extra: dict[str, Any] = {'params': params} if params else {}
        if files:
            extra['files'] = files
        name = f'{route} {label}'.strip()
        with self.client.request(
            method,
            template.format(**(fill or {})),
            json=json,
            data=data,
            headers=sent,
            name=name,
            catch_response=True,
            **extra,
        ) as resp:
            if resp.status_code in expect:
                resp.success()
            elif resp.status_code == 429:
                resp.failure('rate limited (429)')
            elif resp.status_code == 401:
                resp.failure('session lost (401)')
            elif resp.status_code == 0:
                resp.failure(f'no response ({resp.error})')
            else:
                resp.failure(f'HTTP {resp.status_code}')
        if resp.status_code == 401 and relogin_on_401:
            self._login()
        return resp

    def _remember_files(self, resp: Any) -> None:
        uuids = [row['uuid'] for row in _items(resp, 'items') if row.get('uuid')]
        if uuids:
            self.file_uuids = uuids
            FILES_BY_OWNER[self.email] = uuids

    def _a_file(self) -> str | None:
        return random.choice(self.file_uuids) if self.file_uuids else None

    def _collection_roundtrip(self) -> None:
        """Create a collection, fill it with two files, read it, empty it and delete it."""
        if len(self.file_uuids) < 2:
            return
        resp = self._call(
            'collections.create',
            json={'name': f'Load test {uuid.uuid4().hex[:8]}', 'is_public': False},
        )
        if resp.status_code != 200:
            return
        fill = {'collection_uuid': resp.json()['uuid']}
        members = {'media_file_ids': random.sample(self.file_uuids, 2)}
        # 409 is the app's correct answer for a file under a legal hold (content hold).
        self._call('collections.add_media', fill=fill, json=members, expect=(200, 409))
        self._call('collections.media', fill=fill, params={'page': 1, 'page_size': 20})
        self._call('collections.remove_media', fill=fill, json=members)
        self._call('collections.delete', fill=fill, expect=(200, 204))

    def _sleep(self, seconds: float) -> None:
        """Wait inside a task (gevent-friendly: Locust patches ``time.sleep``)."""
        time.sleep(seconds)

    def _fire(
        self, kind: str, name: str, started: float, length: int = 0, error: Any = None
    ) -> None:
        """Record a measurement that is not one HTTP request (a stream, a socket, a pipeline).

        Locust's documented hook for custom clients (``events.request``); the entry lands in
        the same statistics, CSV and percentile history as the HTTP requests.
        """
        self.environment.events.request.fire(
            request_type=kind,
            name=name,
            response_time=(time.perf_counter() - started) * 1000,
            response_length=length,
            exception=error,
            context={},
        )
