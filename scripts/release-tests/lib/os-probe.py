"""Read-mostly probes the rehearsal runs INSIDE a backend/worker container (stdin to `python -`).

lib/feature-checks.sh pipes this file into `docker exec -i <container> python - <command> ...`
so the rehearsal can look at the stores the API does not expose: the real OpenSearch chunk and
speaker indices, and the worker's GPU / audio stack. It prints ONE JSON object on stdout and
decides nothing: every pass/fail verdict lives in lib/feature-checks.py, where it has a good and a
bad case. An error is reported as {"error": "..."} (exit 0), never as a traceback the shell
would have to interpret.

Commands
    labels FILE_UUID       distinct speaker labels the chunk index holds for the file
    drift FILE_UUID        the app's own drift scan, READ-ONLY (nothing is queued), over the whole
                           library, plus the expected/indexed label sets of FILE_UUID
    seed-docs SPEAKER_UUID make sure an UNSTAMPED speaker document exists for the speaker in
                           every concrete speaker index, plus a decoy no scope contains
    count-docs ID...       how many speaker-index documents exist per id (all indices, one count)
    delete-doc ID          remove one document by id (cleanup of the decoy)
    gpu                    CUDA visibility, torchcodec/torchaudio import, and whether the app's
                           own audio loader pulled torchcodec in
"""

from __future__ import annotations

import json
import sys
import tempfile
import uuid
import wave


def _labels(file_uuid: str) -> dict:
    from app.services.privacy.label_reindex import indexed_speaker_labels

    held = indexed_speaker_labels([file_uuid])
    return {'labels': sorted(held.get(file_uuid, set())), 'indexed': file_uuid in held}


def _drift(file_uuid: str) -> dict:
    from app.db.base import SessionLocal
    from app.models.media import MediaFile
    from app.services.privacy import label_reindex
    from app.services.search.reindex_dispatch import ReindexDispatch
    from app.services.system_settings_service import set_setting

    db = SessionLocal()
    try:
        media = db.query(MediaFile).filter(MediaFile.uuid == uuid.UUID(file_uuid)).first()
        if media is None:
            return {'error': f'no media_file {file_uuid}'}
        set_setting(db, label_reindex.DRIFT_CURSOR_KEY, 0, 'release rehearsal probe')
        outcome = label_reindex.detect_label_drift(
            db,
            reindex=lambda **_kw: ReindexDispatch.ALREADY_PENDING,  # counts, queues nothing
            facts=lambda _file_id: None,
        )
        expected = label_reindex._expected_labels(db, [int(media.id)])[int(media.id)]
        held = label_reindex.indexed_speaker_labels([file_uuid]).get(file_uuid, set())
        return {
            'scanned': outcome.scanned,
            'drifted': outcome.drifted,
            'expected': sorted(expected),
            'indexed': sorted(held),
        }
    finally:
        db.close()


def _client():
    from app.services.opensearch_service import opensearch_client

    if opensearch_client is None:
        raise RuntimeError('OpenSearch client unavailable')
    return opensearch_client


def _concrete_speaker_indices(client) -> list[str]:
    from app.core.constants import get_speaker_index_v3, get_speaker_index_v4

    # v3 / v4 only: the alias is the same documents, and the backup is legacy-only.
    return [
        i
        for i in (get_speaker_index_v3(), get_speaker_index_v4())
        if client.indices.exists(index=i)
    ]


def _seed_docs(speaker_uuid: str) -> dict:
    client = _client()
    seeded: list[str] = []
    for index in _concrete_speaker_indices(client):
        if client.exists(index=index, id=speaker_uuid):
            client.update(
                index=index,
                id=speaker_uuid,
                body={'script': {'source': "ctx._source.remove('organization_id')"}},
                refresh=True,
            )
            how = 'existing'
        else:
            client.index(index=index, id=speaker_uuid, body={'user_id': 0}, refresh=True)
            how = 'created'
        seeded.append(f'{index}:{how}')
    decoy = f'rehearsal-decoy-{uuid.uuid4()}'
    indices = _concrete_speaker_indices(client)
    if indices:
        client.index(index=indices[-1], id=decoy, body={'user_id': 0}, refresh=True)
    return {'seeded': seeded, 'decoy': decoy if indices else ''}


def _count_docs(ids: list[str]) -> dict:
    from app.services.gdpr_erasure_service import count_speaker_docs_by_ids

    counts: dict[str, int | None] = {}
    for doc_id in ids:
        counts[doc_id] = count_speaker_docs_by_ids([doc_id])
    return {'counts': counts}


def _delete_doc(doc_id: str) -> dict:
    from app.services.gdpr_erasure_service import delete_speaker_docs_by_ids

    return {'deleted': delete_speaker_docs_by_ids([doc_id])}


def _gpu() -> dict:
    out: dict = {}
    try:
        import torch

        out['cuda_available'] = bool(torch.cuda.is_available())
        out['device_count'] = int(torch.cuda.device_count())
        out['device_name'] = torch.cuda.get_device_name(0) if out['cuda_available'] else ''
        if out['cuda_available']:
            torch.zeros(1, device='cuda')  # a real allocation, not just visibility
    except Exception as exc:  # noqa: BLE001
        out['torch_error'] = f'{type(exc).__name__}: {exc}'[:200]
    for name in ('torchcodec', 'torchaudio'):
        try:
            __import__(name)
            out[f'{name}_import'] = 'ok'
        except Exception as exc:  # noqa: BLE001
            out[f'{name}_import'] = f'{type(exc).__name__}: {exc}'[:200]
    try:
        from app.transcription.audio import load_audio

        sys.modules.pop('torchcodec', None)
        with tempfile.NamedTemporaryFile(suffix='.wav') as handle:
            with wave.open(handle.name, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b'\x00\x00' * 16000)
            samples = load_audio(handle.name)
        out['audio_samples'] = int(len(samples))
        out['app_loader_pulled_torchcodec'] = 'torchcodec' in sys.modules
    except Exception as exc:  # noqa: BLE001
        out['audio_error'] = f'{type(exc).__name__}: {exc}'[:200]
    return out


def main(argv: list[str]) -> int:
    commands = {
        'labels': lambda a: _labels(a[0]),
        'drift': lambda a: _drift(a[0]),
        'seed-docs': lambda a: _seed_docs(a[0]),
        'count-docs': lambda a: _count_docs(a),
        'delete-doc': lambda a: _delete_doc(a[0]),
        'gpu': lambda _a: _gpu(),
    }
    try:
        result = commands[argv[1]](argv[2:])
    except Exception as exc:  # noqa: BLE001 - reported as data; the predicate decides
        result = {'error': f'{type(exc).__name__}: {exc}'[:300]}
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
