"""Concurrent-user load scenario for OpenTranscribe (local ``--fresh`` stacks only).

    locust -f scripts/perf/load/locustfile.py --fresh perf1337 --headless \
        --users 100 --spawn-rate 2 --run-time 15m --csv <run dir>/locust --html <run dir>/report.html

Simulated people log in as the seeded perf identities (``perf-N@loadtest.invalid``, from
``scripts/seed-query-review.sh``) with the SPA's own cookie + CSRF flow, then browse: the
gallery and its filters, a file with its transcript and comments, search, the speakers page
and the task list, with an occasional comment or upload. Writes clean up after themselves.
The runbook (what to run and what to record) is ``scripts/perf/load/README.md``.

Every request goes through :data:`scenario.ROUTES`, which
``backend/tests/unit/test_load_scenario_routes.py`` checks against ``backend/openapi.json``.
"""

from __future__ import annotations

import itertools
import logging
import random
import sys
from typing import Any

import scenario
from locust import HttpUser, between, events, tag, task

logger = logging.getLogger('ot-load')
_ordinary = itertools.count()


@events.init_command_line_parser.add_listener
def _add_arguments(parser: Any) -> None:
    parser.add_argument('--fresh', default='', help='the ./opentr.sh --fresh deployment to load')
    parser.add_argument(
        '--seeded-users', type=int, default=1000, help='users the seed created (its --users)'
    )
    parser.add_argument(
        '--request-timeout', type=float, default=60.0, help='seconds before a call counts as failed'
    )


@events.init.add_listener
def _guard(environment: Any, **_kwargs: Any) -> None:
    options = environment.parsed_options
    if options is None:  # imported as a library (locust --list still parses options)
        return
    try:
        target = scenario.resolve_target(options.fresh, options.host or None)
    except scenario.NotIsolatedError as exc:
        logger.error('%s', exc)
        sys.exit(2)
    environment.host = target.base_url
    logger.info('Loading --fresh %s at %s', target.fresh, target.base_url)


class _SeededUser(HttpUser):
    """One signed-in person. Subclasses pick the identity."""

    abstract = True
    wait_time = between(3, 10)  # think time between page actions

    def identity_index(self) -> int:
        raise NotImplementedError

    def on_start(self) -> None:
        options = self.environment.parsed_options
        self.timeout = options.request_timeout if options else 60.0
        self.seeded_users = options.seeded_users if options else 1000
        self.email = scenario.identity(self.identity_index())
        self.file_uuids: list[str] = []
        self._call('session', label='anonymous probe')
        self._login()
        self._remember_files(self._call('files.list', params={'page': 1, 'page_size': 20}))

    def _login(self) -> None:
        self.client.cookies.clear()
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
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        label: str = '',
        expect: tuple[int, ...] = (200,),
        relogin_on_401: bool = True,
    ) -> Any:
        method, template = scenario.ROUTES[route]
        headers = {}
        if method not in ('GET', 'HEAD'):
            headers['X-CSRF-Token'] = self.client.cookies.get('csrf_token', '')
        name = f'{route} {label}'.strip()
        with self.client.request(
            method,
            template.format(**(fill or {})),
            params=params,
            json=json,
            data=data,
            headers=headers,
            name=name,
            timeout=self.timeout,
            catch_response=True,
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
        if resp.status_code != 200:
            return
        try:
            items = resp.json().get('items') or []
        except ValueError:
            return
        uuids = [item['uuid'] for item in items if item.get('uuid')]
        if uuids:
            self.file_uuids = uuids

    def _a_file(self) -> str | None:
        return random.choice(self.file_uuids) if self.file_uuids else None

    # --- browsing -------------------------------------------------------------------------

    @task(10)
    def gallery(self) -> None:
        self._remember_files(self._call('files.list', params={'page': 1, 'page_size': 20}))

    @task(4)
    def gallery_next_page(self) -> None:
        page = random.randint(2, 5)
        self._call('files.list', params={'page': page, 'page_size': 20}, label='page 2-5')

    @task(6)
    def gallery_filtered(self) -> None:
        k = random.randint(1, 1000)
        choice = random.choice(('search', 'tag', 'speaker', 'sort'))
        params: dict[str, Any] = {'page': 1, 'page_size': 20}
        if choice == 'search':
            params['search'] = f'loadtest-{k}'
        elif choice == 'tag':
            params['tag'] = f'loadtest-tag-{k}'
        elif choice == 'speaker':
            params['speaker'] = f'Perf Person {k}'
        else:
            params['sort_by'] = random.choice(scenario.SORT_FIELDS)
        self._call('files.list', params=params, label=f'filter={choice}')

    @task(2)
    def filter_panel(self) -> None:
        self._call('files.metadata_filters')
        self._call('files.owners')
        self._call('tags.list')
        self._call('collections.list')

    @task(6)
    def open_file(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        fill = {'file_uuid': file_uuid}
        self._call('files.detail', fill=fill, params={'segment_limit': 100})
        self._call('comments.list', fill=fill)
        if random.random() < 0.3:  # scrolls the transcript
            self._call(
                'files.segments', fill=fill, params={'segment_limit': 100, 'segment_offset': 100}
            )

    @task(3)
    def search(self) -> None:
        self._call('search', params={'q': f'loadtest {random.randint(1, 1000)}'})

    @task(2)
    def speakers_page(self) -> None:
        self._call(
            'speakers.for_filter', params={'for_filter': 'true', 'q': ''}, label='for_filter'
        )
        self._call('profiles.list')

    @task(2)
    def tasks_page(self) -> None:
        self._call('tasks.list', params={'page': 1, 'page_size': 20})

    # --- occasional writes (each cleans up after itself) ----------------------------------

    @tag('write')
    @task(1)
    def comment(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        resp = self._call(
            'comments.create',
            fill={'file_uuid': file_uuid},
            json={'text': 'load test comment', 'timestamp': 1.0},
            expect=(200, 201),
        )
        if resp.status_code in (200, 201):
            self._call(
                'comments.delete',
                fill={'comment_uuid': resp.json()['uuid']},
                expect=(200, 204),
            )

    @tag('write', 'upload')
    @task(1)
    def upload(self) -> None:
        """Start an upload (the DB row) and discard it.

        Deliberately never completes it: ``/api/files/complete`` dispatches transcription to
        the stack's GPU workers, which is not what a web-tier load test measures.
        """
        resp = self._call(
            'files.prepare',
            json={
                'filename': f'load-{random.getrandbits(48):012x}.mp3',
                'file_size': 1_048_576,
                'content_type': 'audio/mpeg',
            },
        )
        if resp.status_code != 200:
            return
        file_uuid = resp.json().get('file_id')
        if file_uuid and not resp.json().get('is_duplicate'):
            fill = {'file_uuid': file_uuid}
            self._call('files.delete', fill=fill, expect=(200, 204))
            self._call('files.purge', fill=fill, expect=(200, 204))


class OrdinaryUser(_SeededUser):
    """The bulk of the crowd: perf-3 .. perf-<seeded users>, one identity each."""

    weight = 100

    def identity_index(self) -> int:
        return scenario.ordinary_index(next(_ordinary), self.seeded_users)


class HeavyOwner(_SeededUser):
    """The one account that owns 20,000 files (perf-2)."""

    fixed_count = 1

    def identity_index(self) -> int:
        return scenario.HEAVY_INDEX


class Administrator(_SeededUser):
    """The admin (perf-1): browses like anyone, and opens the statistics pages."""

    fixed_count = 1

    def identity_index(self) -> int:
        return scenario.ADMIN_INDEX

    @task(1)
    def statistics(self) -> None:
        self._call('admin.stats')
        self._call('system.stats')
