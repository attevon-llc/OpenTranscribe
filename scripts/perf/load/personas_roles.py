"""People as they actually use the app: a researcher, a speaker curator, a team lead and the
background tabs, plus the two crowd behaviours the load shapes need (a login herd, a hot file).

``personas_read`` and ``personas_write`` are organised by what they exercise (reads, writes,
chat); these are organised by who is at the keyboard. Their weights are the realistic mix the
``people`` profiles use (README, "Who the people are"). Every task goes through ``routes.ROUTES``
like the older personas, and every write undoes itself in the same task.

Search terms and chat questions come from ``phrasebook.Phrasebook``: learned from the
transcripts, speaker names and tags the person's own pages returned, never random strings.
"""

from __future__ import annotations

import itertools
import random
from typing import Any

import scenario
from locust import between, tag, task
from personas_base import FILES_BY_OWNER, SeededUser, _items
from personas_write import ChatUser, SpeakerWork
from phrasebook import Phrasebook
from ws_listener import WebSocketListener

# The endpoint's own list (VALID_SUMMARY_EXPORT_FORMATS, app/services/summary_export_service.py):
# asking for pdf or docx is a 400 by design, and a unit test keeps the two equal.
SUMMARY_FORMATS = ('md',)
# DOCX and PDF are a different endpoint (POST /export/document, an async render on the download
# worker), whose request schema is TranscriptDocumentExportRequest.format.
DOCUMENT_FORMATS = ('pdf', 'docx')


class CanaryMixin(SeededUser):
    """Asks for a file that belongs to somebody else and expects to be refused.

    A 200 is recorded as a failure of the ``files.detail cross-tenant canary`` row, which
    ``summarize.py`` holds to zero. Files come from what other simulated users have listed in
    this process, so the canary is silent until a second identity has signed in.
    """

    abstract = True
    _readable: set[str] | None = None

    def _readable_files(self) -> set[str]:
        """Every file this caller can read: its own listing plus every collection it owns or is
        shared (``/shared-with-me`` also lists group shares), paged to the end.

        A file in a shared collection answers 200 by design, so the canary must not ask for one.
        Read once per user; labelled so the setup requests stay out of the canary row (whose name holds the word canary).
        """
        if self._readable is None:
            seen = set(self.file_uuids)
            rows = _items(self._call('collections.list', label='readable-set setup'))
            rows += _items(self._call('collections.shared_with_me', label='readable-set setup'))
            for row in rows:
                page, more = 1, True
                while more and row.get('uuid'):
                    resp = self._call(
                        'collections.media',
                        fill={'collection_uuid': row['uuid']},
                        params={'page': page, 'page_size': 100},
                        label='readable-set setup',
                    )
                    seen.update(f['uuid'] for f in _items(resp, 'items') if f.get('uuid'))
                    more = resp.status_code == 200 and bool(resp.json().get('has_more'))
                    page += 1
            self._readable = seen
        return self._readable

    @task(1)
    def cross_tenant_canary(self) -> None:
        candidates = [
            f for owner, files in list(FILES_BY_OWNER.items()) if owner != self.email for f in files
        ]
        if not candidates:
            return
        readable = self._readable_files()
        foreign = [f for f in candidates if f not in readable]
        if foreign:
            self._call(
                'files.detail',
                fill={'file_uuid': random.choice(foreign)},
                expect=(403, 404),
                label='cross-tenant canary',
            )


class Researcher(CanaryMixin, ChatUser):
    """Searches their library, opens transcripts, and (a few of them) chats over a collection.

    Chat turns are limited to 20 a minute per client address, so only the first ``chat_budget``
    researchers chat (``--chat-users``); the rest search and read.
    """

    weight = 40
    fixed_count = 0
    pool = 'viewer'
    wait_time = between(5, 20)
    chat_budget = 3  # set from --chat-users by locustfile._guard
    _slots = itertools.count()

    def on_start(self) -> None:
        super().on_start()
        self.book = Phrasebook()
        self.may_chat = next(Researcher._slots) < Researcher.chat_budget
        self.collection_uuids = [
            row['uuid'] for row in _items(self._call('collections.list')) if row.get('uuid')
        ]
        names = [row.get('name') for row in _items(self._call('tags.list'))]
        names += [row.get('name') for row in _items(self._call('collections.list'))]
        self.book.learn_names(n for n in names if isinstance(n, str))
        self._learn_from(self._a_file())

    def _learn_from(self, file_uuid: str | None) -> list[dict[str, Any]]:
        """Read one page of a transcript and add its words (and speakers) to the phrasebook."""
        if file_uuid is None:
            return []
        page = self._call(
            'files.segments', fill={'file_uuid': file_uuid}, params={'segment_limit': 100}
        )
        segments = _items(page, 'transcript_segments')
        self.book.learn_texts(str(s.get('text', '')) for s in segments)
        self.book.learn_names(
            str(s['speaker'].get('display_name') or s['speaker'].get('name') or '')
            for s in segments
            if isinstance(s.get('speaker'), dict)
        )
        return segments

    @task(10)
    def search_session(self) -> None:
        """Type a term (suggestions), search it, count the hits, then refine once."""
        term = self.book.term()
        self._call('search.suggestions', params={'q': self.book.prefix()})
        mode = random.choice(('keyword', 'keyword', 'hybrid'))
        self._call('search', params={'q': term, 'search_mode': mode}, label=mode)
        self._call('search.count', params={'q': term})
        if random.random() < 0.4:
            other = 'hybrid' if mode == 'keyword' else 'keyword'
            self._call('search', params={'q': self.book.term(), 'search_mode': other}, label=other)

    @task(6)
    def read_transcript(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        fill = {'file_uuid': file_uuid}
        self._call('files.detail', fill=fill, params={'segment_limit': 100})
        self._learn_from(file_uuid)
        self._call('comments.list', fill=fill)
        self._call('speakers.list', params={'file_uuid': file_uuid}, label='for file')
        self._call('files.analytics', fill=fill)
        self._call('files.waveform', fill=fill, params={'samples': 500})

    @task(3)
    def find_in_gallery(self) -> None:
        self._call(
            'files.list',
            params={'page': 1, 'page_size': 20, 'search': self.book.term()},
            label='phrase',
        )

    @task(2)
    def browse_collection(self) -> None:
        if self.collection_uuids:
            fill = {'collection_uuid': random.choice(self.collection_uuids)}
            self._call('collections.media', fill=fill, params={'page': 1, 'page_size': 20})

    def _scope(self) -> dict[str, list[str]]:
        """Mostly a whole collection (how a researcher asks across recordings), else a few files."""
        if self.collection_uuids and random.random() < 0.7:
            return {'collection_uuids': [random.choice(self.collection_uuids)]}
        return super()._scope()

    def _question(self) -> str:
        return self.book.question()


class SpeakerCurator(CanaryMixin, SpeakerWork):
    """Works the speaker inbox: reviews clusters and suggestions, names and merges speakers."""

    weight = 8
    pool = 'editor'
    file_filter = {'held': 'false'}
    wait_time = between(5, 15)

    @task(5)
    def inbox(self) -> None:
        self._call('clusters.inbox', params={'page': 1, 'per_page': 20})
        self._call('clusters.stats')
        self._call('clusters.list')

    @task(4)
    def inspect_speaker(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        speakers = _items(
            self._call('speakers.list', params={'file_uuid': file_uuid}, label='for file')
        )
        if not speakers:
            return
        fill = {'speaker_uuid': random.choice(speakers)['uuid']}
        # 404: a speaker with no voiceprint yet has nothing to suggest or compare.
        self._call('speakers.suggestions', fill=fill, expect=(200, 404))
        self._call('speakers.history', fill=fill, expect=(200, 404))
        self._call('speakers.cross_media', fill=fill, expect=(200, 404))

    @tag('write')
    @task(2)
    def name_and_merge(self) -> None:
        self._temporary_speaker()


class TeamLead(CanaryMixin, SeededUser):
    """Watches the team's work: gallery and collections, shares, summaries, exports."""

    weight = 12
    pool = 'editor'
    file_filter = {'held': 'false'}
    wait_time = between(8, 25)

    def on_start(self) -> None:
        super().on_start()
        self.collection_uuids = [
            row['uuid'] for row in _items(self._call('collections.list')) if row.get('uuid')
        ]

    @task(5)
    def dashboard(self) -> None:
        self._remember_files(self._call('files.list', params={'page': 1, 'page_size': 20}))
        self._call('collections.list')
        self._call('collections.shared_with_me')
        self._call('tags.list')
        self._call('tasks.active')
        self._call('my_files.status')

    @task(3)
    def review_collection(self) -> None:
        if self.collection_uuids:
            fill = {'collection_uuid': random.choice(self.collection_uuids)}
            self._call('collections.media', fill=fill, params={'page': 1, 'page_size': 20})
            self._call('collections.shares', fill=fill, params={'skip': 0, 'limit': 50})

    @task(4)
    def read_summary(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        fill = {'file_uuid': file_uuid}
        self._call('files.summary_status', fill=fill)
        # 404: a seeded file has no summary, a valid answer.
        self._call('summary.get', fill=fill, expect=(200, 404))
        self._call('topics.suggestions', fill=fill, expect=(200, 204))

    @task(2)
    def export_pack(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        fill = {'file_uuid': file_uuid}
        fmt = random.choice(SUMMARY_FORMATS)
        self._call(
            'summary.export', fill=fill, params={'format': fmt}, expect=(200, 404), label=fmt
        )
        self._call('files.export', fill=fill, params={'format': 'txt'}, label='txt')
        self._call('files.captions', fill=fill)

    @tag('write')  # queues a render job, so a read-only run must not send it
    @task(1)  # low weight: every call loads the download worker
    def export_document(self) -> None:
        """Queue a DOCX or PDF export, as the export dialog does (requestDocumentExport.ts).

        The browser then opens the job stream for the presigned URL; the render runs on the
        worker whether or not anyone listens, so the load is the queued job. 409 is the app's
        answer while redaction detection has not finished, 404 a file with no transcript.
        """
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        fmt = random.choice(DOCUMENT_FORMATS)
        body = {
            'format': fmt,
            'include_comments': False,
            'include_timestamps': True,
            'include_speakers': True,
            'line_numbering': 'per_page',
            'page_size': 'a4',
        }
        self._call(
            'files.export_document',
            fill={'file_uuid': file_uuid},
            json=body,
            expect=(202, 404, 409),
            label=fmt,
        )

    @tag('write')
    @task(1)
    def curate_collection(self) -> None:
        self._collection_roundtrip()


class StatusPoller(SeededUser):
    """A tab left open on the upload page: the SPA's status polling, with no one at the keyboard."""

    weight = 12
    pool = 'ws'
    wait_time = between(5, 10)  # the SPA falls back to polling while its socket is down

    @task(4)
    def poll_active(self) -> None:
        self._call('tasks.active')
        self._call('my_files.status')

    @task(1)
    def poll_one_file(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is not None:
            self._call('files.status_detail', fill={'file_uuid': file_uuid})

    @task(1)
    def keep_session(self) -> None:
        self._call('me')
        self._call('system.capabilities')


class WatcherBot(WebSocketListener):
    """A background tab holding the live socket; the older ``WebSocketListener`` with a mix weight."""

    weight = 18


class LoginHerd(SeededUser):
    """Many people signing in at once: the SPA's boot sequence, then signing in again, fast."""

    weight = 1
    pool = 'viewer'
    wait_time = between(1, 3)

    @task
    def sign_in_again(self) -> None:
        self._login()
        self._call('system.capabilities')
        self._call('files.list', params={'page': 1, 'page_size': 20}, label='first page')


class HotFileReader(SeededUser):
    """Everyone on one file: many sessions of one account reading (and annotating) its first file."""

    weight = 1
    wait_time = between(1, 4)

    def identity_index(self) -> int:
        return scenario.HEAVY_INDEX

    def on_start(self) -> None:
        super().on_start()
        # The same account lists the same first page for every session, so every session lands
        # on the same file with no coordination.
        resp = self._call(
            'files.list',
            params={'page': 1, 'page_size': 1, 'sort_by': 'upload_time', 'sort_order': 'asc'},
            label='hot file',
        )
        self.hot = (_items(resp, 'items') or [{}])[0].get('uuid')

    @task(6)
    def read_hot_file(self) -> None:
        if self.hot:
            fill = {'file_uuid': self.hot}
            self._call('files.detail', fill=fill, params={'segment_limit': 100}, label='hot')
            self._call('files.segments', fill=fill, params={'segment_limit': 100}, label='hot')
            self._call('comments.list', fill=fill, label='hot')
            self._call('files.analytics', fill=fill, label='hot')

    @tag('write')
    @task(1)
    def annotate_hot_file(self) -> None:
        """Everyone writing the same rows is where a deadlock would show."""
        if not self.hot:
            return
        resp = self._call(
            'comments.create',
            fill={'file_uuid': self.hot},
            json={'text': 'hot file comment', 'timestamp': 1.0},
            expect=(200, 201),
            label='hot',
        )
        if resp.status_code in (200, 201):
            self._call(
                'comments.delete',
                fill={'comment_uuid': resp.json()['uuid']},
                expect=(200, 204),
                label='hot',
            )
