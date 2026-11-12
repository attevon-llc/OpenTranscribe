"""Personas that change things: the editor, the chat user and the pipeline uploader.

Every write touches only the signed-in person's own files (the gallery's default
``ownership=mine``) and is undone inside the same task: a comment is created, edited and
deleted; a tag is attached and deleted; a collection is created, filled, emptied and deleted; a
segment's text is changed and restored; a temporary speaker is created, given a segment,
renamed, handed the segment back and merged away; a file is hidden and shown again; an upload
is deleted and purged once it finishes; a chat and the mock-LLM configurations are deleted.
``reprocess`` re-runs only the analytics stage, which recomputes the same numbers from the same
segments.

Tasks tagged ``write`` change data; ``gpu`` marks the editor's real upload, which sends a
recording through the transcription pipeline (``run.sh --mix`` chooses which run).
"""

from __future__ import annotations

import random
import time
import uuid
from typing import Any

import routes
import scenario
from locust import between, constant, tag, task
from personas_base import SeededUser, _items

TERMINAL = frozenset({'completed', 'error', 'failed', 'cancelled', 'orphaned'})
_AUDIO: bytes | None = None


def _sample_audio() -> bytes:
    global _AUDIO
    if _AUDIO is None:
        _AUDIO = scenario.SAMPLE_AUDIO.read_bytes()
    return _AUDIO


class SpeakerWork(SeededUser):
    """The speaker edit both the editor and the speaker curator make."""

    abstract = True

    def _temporary_speaker(self) -> None:
        """Create a speaker, give it a segment, rename it, hand the segment back, merge it away.

        The merge returns the segment to its original speaker and removes the temporary
        speaker, so the file ends as it started.
        """
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        self._call('speakers.list', params={'file_uuid': file_uuid}, label='for file')
        page = self._call(
            'files.segments', fill={'file_uuid': file_uuid}, params={'segment_limit': 50}
        )
        segments = [
            s
            for s in _items(page, 'transcript_segments')
            if s.get('uuid') and isinstance(s.get('speaker'), dict) and s['speaker'].get('uuid')
        ]
        if not segments:
            return
        segment = random.choice(segments)
        original = segment['speaker']['uuid']
        created = self._call(
            'speakers.create',
            params={'media_file_uuid': file_uuid},
            json={'name': f'LOADTEST_{uuid.uuid4().hex[:6]}'},
        )
        if created.status_code != 200:
            return
        temp = created.json()['uuid']
        moved = {'segment_uuid': segment['uuid']}
        self._call('segments.speaker', fill=moved, json={'speaker_uuid': temp})
        self._call(
            'speakers.update', fill={'speaker_uuid': temp}, json={'display_name': 'Load Test'}
        )
        # Restore by merging the temp speaker into the original: the merge moves its segment
        # back and removes it. Moving the segment back first deletes the now-empty temp speaker
        # (transcript_segments.py), so the merge then 404s.
        self._call('speakers.merge', fill={'speaker_uuid': temp, 'target_speaker_uuid': original})


class _Uploads(SpeakerWork):
    """Uploading a real recording and waiting for the pipeline to finish it."""

    abstract = True

    def _upload_and_wait(self) -> None:
        """Prepare, upload, poll until a terminal status, then delete and purge the file.

        Records ``PIPELINE upload->completed`` (end-to-end seconds, as milliseconds) in the
        statistics, as a failure when the file ended in error or did not finish in time.
        """
        options = self.environment.parsed_options
        deadline_s = options.pipeline_timeout if options else 900.0
        poll_s = options.pipeline_poll if options else 5.0
        name = f'load-{uuid.uuid4().hex[:12]}.wav'
        body = scenario.unique_wav(_sample_audio(), name)
        prepared = self._call(
            'files.prepare',
            json={'filename': name, 'file_size': len(body), 'content_type': 'audio/wav'},
            label='real upload',
        )
        file_uuid = prepared.json().get('file_id') if prepared.status_code == 200 else None
        if not file_uuid:
            return
        started = time.perf_counter()
        uploaded = self._call(
            'files.upload',
            files={'file': (name, body, 'audio/wav')},
            headers={'X-File-ID': file_uuid, 'X-Skip-Summary': 'true'},
        )
        status, error = 'not uploaded', None
        if uploaded.status_code == 200:
            status = self._wait_for_terminal(file_uuid, started, deadline_s, poll_s)
            if status != 'completed':
                error = RuntimeError(f'ended {status}')
            self._fire('PIPELINE', 'upload->completed', started, len(body), error)
        fill = {'file_uuid': file_uuid}
        self._call('files.delete', fill=fill, expect=(200, 204), label='real upload')
        self._call('files.purge', fill=fill, expect=(200, 202, 204), label='real upload')

    def _wait_for_terminal(
        self, file_uuid: str, started: float, deadline_s: float, poll_s: float
    ) -> str:
        status = 'pending'
        while time.perf_counter() - started < deadline_s:
            self._sleep(poll_s)
            resp = self._call('files.status_detail', fill={'file_uuid': file_uuid})
            if resp.status_code == 200:
                status = str(resp.json().get('status', status))
                if status in TERMINAL:
                    return status
        return f'timed out ({status})'


class Editor(_Uploads):
    """Edits their own files: comments, tags, collections, transcript, speakers, uploads."""

    weight = 10
    pool = 'editor'
    file_filter = {'held': 'false'}  # a held file refuses edits by design

    @task(4)
    def review_file(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is not None:
            fill = {'file_uuid': file_uuid}
            self._call('files.detail', fill=fill, params={'segment_limit': 100})
            self._call('comments.list', fill=fill)

    @tag('write')
    @task(3)
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
            fill = {'comment_uuid': resp.json()['uuid']}
            self._call('comments.update', fill=fill, json={'text': 'load test comment (edited)'})
            self._call('comments.delete', fill=fill, expect=(200, 204))

    @tag('write')
    @task(2)
    def tag_file(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        resp = self._call(
            'tags.add_to_file',
            fill={'file_uuid': file_uuid},
            json={'name': f'loadtest-edit-{uuid.uuid4().hex[:8]}'},
        )
        if resp.status_code == 200:
            self._call('tags.delete', params={'tag_uuids': [resp.json()['uuid']]})

    @tag('write')
    @task(1)
    def collection(self) -> None:
        self._collection_roundtrip()

    @tag('write')
    @task(2)
    def edit_segment(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is None:
            return
        page = self._call(
            'files.segments', fill={'file_uuid': file_uuid}, params={'segment_limit': 50}
        )
        segments = [s for s in _items(page, 'transcript_segments') if s.get('uuid')]
        if not segments:
            return
        segment = random.choice(segments)
        fill = {'file_uuid': file_uuid, 'segment_uuid': segment['uuid']}
        original = segment.get('text') or ''
        self._call('segments.update', fill=fill, json={'text': original + ' (edited)'})
        self._call('segments.update', fill=fill, json={'text': original}, label='restore')

    @tag('write')
    @task(1)
    def speakers(self) -> None:
        self._temporary_speaker()

    @tag('write')
    @task(1)
    def hide_and_show(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is not None:
            fill = {'file_uuid': file_uuid}
            self._call('files.hide', fill=fill, expect=(200, 204))
            self._call('files.unhide', fill=fill, expect=(200, 204))

    @tag('write')
    @task(1)
    def reprocess_analytics(self) -> None:
        file_uuid = self._a_file()
        if file_uuid is not None:
            # 409: the file is already being reprocessed (another task got there first).
            self._call(
                'files.reprocess',
                fill={'file_uuid': file_uuid},
                json={'stages': ['analytics']},
                expect=(200, 409),
            )

    @tag('write')
    @task(1)
    def start_and_abandon_upload(self) -> None:
        """Prepare an upload (the database row) and discard it before any bytes are sent."""
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
            self._call('files.purge', fill=fill, expect=(200, 202, 204))

    @tag('write', 'gpu')
    @task(1)
    def upload_recording(self) -> None:
        self._upload_and_wait()


class PipelineUploader(_Uploads):
    """Only for the ``pipeline`` profile: upload, wait for "completed", clean up, repeat.

    Its weight only keeps Locust from discarding the class at start; the other profiles do not
    list it, so it never joins a web-tier run.
    """

    weight = 1
    pool = 'pipeline'
    wait_time = constant(1)

    @task
    def pipeline(self) -> None:
        self._upload_and_wait()


class ChatUser(SeededUser):
    """Chats about their own recordings through the mock LLM (``--with-mock-llm``).

    The first chat turn creates two personal LLM configurations (``mock-gpt`` for an ordinary
    answer, ``mock-slow`` for a stream held open for most of a minute); ``on_stop`` deletes
    them. A read-only run (``--exclude-tags write``) only reads the chat history.
    """

    weight = 0
    fixed_count = 1  # set from --chat-users at start (chat turns are rate limited per address)
    pool = 'chat'
    wait_time = between(20, 60)

    def on_start(self) -> None:
        super().on_start()
        self.configs: dict[str, str] = {}
        self._call('llm.status')

    def on_stop(self) -> None:
        for model, config_uuid in self.configs.items():
            self._call(
                'llm.delete', fill={'config_uuid': config_uuid}, expect=(200, 204), label=model
            )
        self.configs = {}

    def _config(self, model: str) -> str | None:
        """This person's LLM configuration for ``model``, created on first use."""
        if model not in self.configs:
            options = self.environment.parsed_options
            resp = self._call(
                'llm.create',
                json={
                    'name': f'Load test {model} {uuid.uuid4().hex[:8]}',
                    'provider': 'custom',
                    'model_name': model,
                    'base_url': options.mock_llm_url if options else 'http://mock-llm:5199/v1',
                    'api_key': 'mock-key-not-secret',
                    'is_active': False,
                },
                label=model,
            )
            if resp.status_code != 200:
                return None
            self.configs[model] = resp.json()['uuid']
        return self.configs[model]

    @task(3)
    def history(self) -> None:
        listing = _items(self._call('chat.list', params={'limit': 30}), 'conversations')
        if listing:
            fill = {'conversation_uuid': random.choice(listing)['uuid']}
            self._call('chat.get', fill=fill)
            self._call('chat.messages', fill=fill, params={'limit': 100})

    @tag('write')
    @task(3)
    def chat_turn(self) -> None:
        self._turn('mock-gpt')

    @tag('write')
    @task(1)
    def long_stream(self) -> None:
        self._turn('mock-slow')

    def _turn(self, model: str) -> None:
        config = self._config(model)
        if config is None or not self.file_uuids:
            return
        created = self._call(
            'chat.create',
            json={'title': 'Load test', 'scope': self._scope(), 'llm_config_uuid': config},
            expect=(200, 201),
            label=model,
        )
        if created.status_code not in (200, 201):
            return
        fill = {'conversation_uuid': created.json()['uuid']}
        self._stream(fill, model)
        self._call('chat.delete', fill=fill, expect=(200, 204), label=model)

    def _scope(self) -> dict[str, list[str]]:
        """What a chat is about: up to three of the person's own files."""
        return {'file_uuids': random.sample(self.file_uuids, min(3, len(self.file_uuids)))}

    def _question(self) -> str:
        return 'What did they decide about the budget?'

    def _stream(self, fill: dict[str, str], model: str) -> None:
        """Send one question and read the answer's server-sent events to the end.

        Records ``SSE <model> first token`` and ``SSE <model> whole answer``. The slow model
        stalls past the app's first-token watchdog, so its ``error`` frame is the designed
        outcome and is not a failure; for ``mock-gpt`` it is.
        """
        started = time.perf_counter()
        events: list[str] = []
        error: Any = None
        first = False
        try:
            for line in self.client.iter_lines(
                routes.ROUTES['chat.send'][1].format(**fill),
                method='POST',
                json={'content': self._question()},
                headers=self._csrf_header(),
                name=f'chat.send {model}',
            ):
                if line.startswith('event: '):
                    event = line[len('event: ') :].strip()
                    events.append(event)
                    if event == 'delta' and not first:
                        first = True
                        self._fire('SSE', f'{model} first token', started)
        except (OSError, ValueError) as exc:  # a refused or broken stream: a failed answer
            error = exc
        if error is None and 'done' not in events and model == 'mock-gpt':
            error = RuntimeError(f'no done frame (events: {sorted(set(events))})')
        if error is None and model == 'mock-slow' and not {'done', 'error'} & set(events):
            error = RuntimeError('stream ended without done or error')
        self._fire('SSE', f'{model} whole answer', started, len(events), error)

    def _csrf_header(self) -> dict[str, str]:
        return {'X-CSRF-Token': scenario.cookie_value(self.client.cookiejar, 'csrf_token')}
