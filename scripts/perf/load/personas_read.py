"""Personas that read: the viewer (most of the crowd), the heavy owner, both kinds of admin.

Every id a task uses comes from a listing the person's own pages return (the gallery, the tag
and collection lists, the hold list), never from the seed's arithmetic.
"""

from __future__ import annotations

import random
from typing import Any

import routes
import scenario
from locust import task
from locust.exception import StopUser
from personas_base import SeededUser, _items

SORT_FIELDS = ('upload_time', 'duration', 'file_size', 'completed_at', 'filename')
EXPORT_FORMATS = ('txt', 'json', 'csv', 'srt', 'vtt', 'md')
SUBTITLE_FORMATS = ('srt', 'webvtt')
GALLERY = {'page': 1, 'page_size': 20}


class Viewer(SeededUser):
    """Browses their own library: gallery and filters, file pages, search, speakers, settings."""

    weight = 55
    pool = 'viewer'

    def on_start(self) -> None:
        super().on_start()
        self.tag_names = [row['name'] for row in _items(self._call('tags.list')) if row.get('name')]
        self.collection_uuids = [
            row['uuid'] for row in _items(self._call('collections.list')) if row.get('uuid')
        ]
        self.speaker_names = [
            row.get('display_name') or row.get('name')
            for row in _items(
                self._call(
                    'speakers.list', params={'for_filter': 'true', 'q': ''}, label='for_filter'
                )
            )
            if row.get('display_name') or row.get('name')
        ]
        self.profile_uuids = [
            row['uuid'] for row in _items(self._call('profiles.list')) if row.get('uuid')
        ]

    # --- the gallery ----------------------------------------------------------------------

    @task(10)
    def gallery(self) -> None:
        self._remember_files(self._call('files.list', params=GALLERY))

    @task(4)
    def gallery_next_page(self) -> None:
        params = {**GALLERY, 'page': random.randint(2, 5)}
        self._call('files.list', params=params, label='page 2-5')

    @task(8)
    def gallery_filtered(self) -> None:
        choice, extra = random.choice(self._filters())
        self._call('files.list', params={**GALLERY, **extra}, label=f'filter={choice}')

    def _filters(self) -> list[tuple[str, dict[str, Any]]]:
        filters: list[tuple[str, dict[str, Any]]] = [
            ('search', {'search': f'loadtest-{random.randint(1, 1000)}'}),
            ('sort', {'sort_by': random.choice(SORT_FIELDS)}),
            ('held', {'held': 'true'}),
            ('not held', {'held': 'false'}),
            ('hidden only', {'hidden': 'only'}),
            ('hidden include', {'hidden': 'include'}),
            ('shared', {'ownership': 'all'}),
        ]
        if self.tag_names:
            filters.append(('tag', {'tag': random.choice(self.tag_names)}))
        if self.speaker_names:
            filters.append(('speaker', {'speaker': random.choice(self.speaker_names)}))
        return filters

    @task(2)
    def filter_panel(self) -> None:
        self._call('files.metadata_filters')
        self._call('files.owners')
        self._call('tags.list')
        self._call('collections.list')
        self._call('collections.shared_with_me')

    @task(2)
    def collection_page(self) -> None:
        if self.collection_uuids:
            fill = {'collection_uuid': random.choice(self.collection_uuids)}
            self._call('collections.media', fill=fill, params=GALLERY)

    @task(1)
    def trash_page(self) -> None:
        self._call('files.trash', params={'page': 1, 'page_size': 50})

    # --- a file's page --------------------------------------------------------------------

    @task(6)
    def open_file(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        fill = {'file_uuid': file_uuid}
        self._call('files.detail', fill=fill, params={'segment_limit': 100})
        self._call('comments.list', fill=fill)
        self._call('speakers.list', params={'file_uuid': file_uuid}, label='for file')
        self._call('tags.for_files', params={'file_uuids': [file_uuid]})
        self._call('collections.for_files', params={'file_uuids': [file_uuid]})
        self._call('files.analytics', fill=fill)
        self._call('files.summary_status', fill=fill)
        self._call('files.post_processing', fill=fill)
        self._call('topics.suggestions', fill=fill)
        # No summary on a seeded file is a valid answer (404), not a failure.
        self._call('summary.get', fill=fill, expect=(200, 404))
        self._call('files.stream_url', fill=fill, params={'media_type': 'audio'})
        self._call('files.waveform', fill=fill, params={'samples': 500})
        for offset in (100, 200)[: random.randint(0, 2)]:  # scrolls the transcript
            self._call(
                'files.segments', fill=fill, params={'segment_limit': 100, 'segment_offset': offset}
            )

    @task(1)
    def export_file(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        fill = {'file_uuid': file_uuid}
        fmt = random.choice(SUBTITLE_FORMATS)
        self._call('files.subtitles', fill=fill, params={'subtitle_format': fmt}, label=fmt)
        fmt = random.choice(EXPORT_FORMATS)
        self._call('files.export', fill=fill, params={'format': fmt}, label=fmt)
        self._call('files.captions', fill=fill)

    # --- search, speakers, tasks, settings ------------------------------------------------

    @task(3)
    def search(self) -> None:
        word = f'loadtest {random.randint(1, 1000)}'
        self._call('search.suggestions', params={'q': word[:6]})
        self._call('search', params={'q': word})
        self._call('search.count', params={'q': word})

    @task(2)
    def speakers_page(self) -> None:
        self._call('speakers.list', params={'for_filter': 'true', 'q': ''}, label='for_filter')
        self._call('profiles.list')
        self._call('clusters.list')
        self._call('clusters.stats')
        if self.profile_uuids:
            fill = {'profile_uuid': random.choice(self.profile_uuids)}
            self._call('profiles.occurrences', fill=fill)

    @task(2)
    def tasks_page(self) -> None:
        self._call('tasks.list', params={'page': 1, 'page_size': 25})
        self._call('tasks.active')
        self._call('my_files.status')

    @task(1)
    def settings_pages(self) -> None:
        for route in (
            'users.me',
            'settings.all',
            'settings.chat',
            'settings.redaction',
            'settings.diarization',
            'settings.pipeline',
            'settings.asr',
            'settings.llm',
            'vocabulary.list',
            'prompts.list',
            'privacy.me',
            'legal.documents',
            'auth.sessions',
            'usage.me',
            'groups.list',
            'version',
        ):
            self._call(route)


class HeavyOwner(Viewer):
    """The one account that owns 20,000 files (perf-2): every list it opens is the largest."""

    weight = 0
    fixed_count = 1

    def identity_index(self) -> int:
        return scenario.HEAVY_INDEX


class Administrator(Viewer):
    """The admin (perf-1): browses like anyone, and opens the administration pages."""

    weight = 0
    fixed_count = 1

    def identity_index(self) -> int:
        return scenario.ADMIN_INDEX

    def on_start(self) -> None:
        super().on_start()
        holds = _items(self._call('admin.holds', params={'active_only': 'false'}), 'holds')
        self.hold_uuids = [row['uuid'] for row in holds if row.get('uuid')]

    @task(3)
    def statistics(self) -> None:
        self._call('admin.stats')
        self._call('system.stats')
        self._call('tasks.system_health')
        self._call('admin.timing_recent')

    @task(2)
    def people(self) -> None:
        self._call('admin.users', params={'limit': 50})
        self._call('admin.audit_logs', params={'limit': 50})
        self._call('admin.locked_accounts')
        self._call('admin.user_approvals')

    @task(2)
    def governance(self) -> None:
        self._call('admin.quarantined', params={'limit': 50})
        self._call('admin.holds', params={'limit': 50})
        if self.hold_uuids:
            self._call('admin.hold', fill={'hold_uuid': random.choice(self.hold_uuids)})
        self._call('admin.takedowns')
        self._call('files.stuck')
        self._call('search.index_health')

    @task(1)
    def settings(self) -> None:
        for route in (
            'admin.retention_config',
            'admin.retry_config',
            'admin.cache_config',
            'admin.transcription_defaults',
            'admin.chat_settings',
            'admin.redaction_policy',
            'admin.auth_config_status',
            'admin.first_run',
            'admin.group_mappings',
            'admin.scim_tokens',
        ):
            self._call(route)


class OrgAdmin(SeededUser):
    """The first organization's admin (perf-3), on the org-admin pages its edition switches on.

    Those pages are mounted behind capabilities that the community edition turns off (they
    answer 404 there), so this person reads ``/api/system/capabilities`` and stops when none of
    them is on, rather than reporting a page of 404s as load.
    """

    weight = 0
    fixed_count = 1

    def identity_index(self) -> int:
        return scenario.ORG_ADMIN_INDEX

    def on_start(self) -> None:
        super().on_start()
        resp = self._call('system.capabilities', label='org pages')
        body = resp.json() if resp.status_code == 200 else {}
        granted = body.get('capabilities', {}) if isinstance(body, dict) else {}
        self.routes = [
            route for route, cap in routes.ORG_ROUTE_CAPABILITY.items() if granted.get(cap) is True
        ]
        if not self.routes:
            raise StopUser()
        holds = _items(self._call('org.holds'), 'holds') if 'org.holds' in self.routes else []
        self.hold_uuids = [row['uuid'] for row in holds if row.get('uuid')]

    @task
    def org_pages(self) -> None:
        for route in self.routes:
            if route == 'org.hold':
                if self.hold_uuids:
                    self._call(route, fill={'hold_uuid': random.choice(self.hold_uuids)})
            else:
                self._call(route)
