#!/usr/bin/env python3
"""Score BOTH diarization engines against hand-labelled ground truth (issue #669).

**"Same weights" does not imply "same output."** diar-native serves ONNX graphs exported
from the same ``pyannote/speaker-diarization-community-1`` pipeline the in-process fork
runs, but the export, the clustering implementation and the segmentation batching all
differ. Upstream's own ``verify-models`` passes all five of its stages on a build whose
AMI-16 DER is ~52% instead of 18.7%, so a functional smoke test cannot stand in for an
accuracy measurement. This is the measurement.

Both engines are driven through the real ``ModelManager`` path over the SAME decoded audio
and scored with ``pyannote.metrics``. Nothing is mocked.

Run it inside the GPU worker, which has the models, the GPU and the sidecar's URL::

    W=opentranscribe-celery-worker
    CLIP=benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU
    docker exec $W mkdir -p /tmp/der
    docker cp $CLIP/karpathy_10m.wav  $W:/tmp/der/
    docker cp $CLIP/reference.rttm    $W:/tmp/der/
    docker cp scripts/diar-native-der-parity.py $W:/tmp/der/
    docker exec $W python3 /tmp/der/diar-native-der-parity.py

⚠️ **THE TRAP THAT PRODUCES A FAKE CATASTROPHE.** ``reference.rttm`` labels the WHOLE
66-minute source video (3,989 s); ``karpathy_10m.wav`` is only its first 10 minutes.
Scoring the clip against the uncropped reference makes ~85% of reference speech a miss and
reports **DER ≈ 0.86 for BOTH engines** — which reads exactly like a catastrophic accuracy
regression and is entirely an artefact. That happened on the first run here. The crop below
is not tidiness; it is the difference between 0.86 and 0.05. If you point this at a
different clip, re-derive ``--seconds`` from the audio, never from memory.

Measured 2026-09-04 (RTX 3080 Ti, diar-server 0.3.1, 600 s / 2 speakers)::

    engine     segs  spk   DER(0.25)  DER(0)   JER     wall
    native      103    2      0.0515   0.0728  0.0741  3.89 s
    pyannote     88    2      0.0484   0.0704  0.0667  9.33 s

i.e. native is 0.31 percentage points behind on DER — noise at n=1 — for 2.4x the speed.
Quote the command, not these numbers: a figure transcribed into prose rots.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

DEFAULT_APP_ROOT = '/app'


def read_rttm(path: Path):
    """Parse an RTTM into an Annotation.

    Speaker labels in the committed reference contain SPACES ("Andrej Karpathy"), so the
    label is everything from field 8 on — splitting on whitespace and taking ``parts[7]``
    silently truncates it. DER uses an optimal label mapping so a truncation would not
    change the score, but it would make the printed speaker list wrong.
    """
    from pyannote.core import Annotation, Segment

    ann = Annotation(uri=path.stem)
    with path.open(encoding='utf-8') as fh:
        for line in fh:
            parts = line.split()
            if not parts or parts[0] != 'SPEAKER':
                continue
            start, dur = float(parts[3]), float(parts[4])
            label = ' '.join(parts[7:]).replace('<NA>', '').strip() or parts[7]
            ann[Segment(start, start + dur)] = label
    return ann


def to_annotation(result):
    """DiarizeResult -> Annotation, dropping zero/negative-length segments."""
    from pyannote.core import Annotation, Segment

    ann = Annotation(uri='hypothesis')
    for start, end, speaker in zip(result.start, result.end, result.speaker, strict=True):
        if float(end) > float(start):
            ann[Segment(float(start), float(end))] = str(speaker)
    return ann


def run_engine(backend: str, audio) -> dict:
    """Diarize with one backend through the real ModelManager path."""
    from app.transcription.config import TranscriptionConfig
    from app.transcription.model_manager import ModelManager

    config = dataclasses.replace(TranscriptionConfig.from_environment(), diarizer_backend=backend)
    diarizer = ModelManager.get_instance().get_diarizer(config)
    started = time.perf_counter()
    result, _overlap, _embeddings = diarizer.diarize(audio)
    elapsed = time.perf_counter() - started
    return {
        'requested_backend': backend,
        # The class name is the ONLY honest answer to "which engine ran": asking for
        # `native` and silently receiving SpeakerDiarizer is the documented failure mode.
        'engine_class': type(diarizer).__name__,
        'segments': len(result),
        'speakers': sorted({str(s) for s in result.speaker}),
        'elapsed_s': round(elapsed, 2),
        'annotation': to_annotation(result),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', default='/tmp/der/karpathy_10m.wav')  # noqa: S108
    parser.add_argument('--reference', default='/tmp/der/reference.rttm')  # noqa: S108
    parser.add_argument('--json-out', default='')
    parser.add_argument(
        '--seconds',
        type=float,
        default=600.0,
        help='Crop the reference to this many seconds — see the module docstring.',
    )
    parser.add_argument('--app-root', default=DEFAULT_APP_ROOT)
    args = parser.parse_args()

    sys.path.insert(0, args.app_root)
    from pyannote.core import Segment
    from pyannote.metrics.diarization import DiarizationErrorRate, JaccardErrorRate

    from app.transcription.audio import load_audio

    reference = read_rttm(Path(args.reference)).crop(
        Segment(0.0, args.seconds), mode='intersection'
    )
    audio = load_audio(args.audio)
    measured = len(audio) / 16000
    if abs(measured - args.seconds) > 5:
        print(
            f'REFUSING: audio is {measured:.1f}s but --seconds is {args.seconds}. '
            'Scoring a clip against a longer reference reports a fake catastrophe '
            '(~0.86 DER for every engine). Pass the real length.',
            file=sys.stderr,
        )
        return 2
    print(f'reference: {len(reference)} segments, speakers={sorted(reference.labels())}')
    print(f'audio:     {measured:.1f}s, decoded once and shared by both engines\n')

    results: dict[str, dict] = {}
    for backend in ('native', 'pyannote'):
        try:
            record = run_engine(backend, audio)
        except Exception as exc:  # noqa: BLE001 — one engine failing must not hide the other
            print(f'{backend:9s} FAILED {type(exc).__name__}: {exc}')
            results[backend] = {'error': f'{type(exc).__name__}: {exc}'}
            continue
        hypothesis = record.pop('annotation')
        record['der_collar250ms'] = round(
            float(DiarizationErrorRate(collar=0.25, skip_overlap=True)(reference, hypothesis)), 4
        )
        record['der_collar0'] = round(
            float(DiarizationErrorRate(collar=0.0, skip_overlap=False)(reference, hypothesis)), 4
        )
        record['jer'] = round(
            float(JaccardErrorRate(collar=0.25, skip_overlap=True)(reference, hypothesis)), 4
        )
        results[backend] = record
        print(
            f'{backend:9s} engine={record["engine_class"]:22s} segs={record["segments"]:4d} '
            f'spk={len(record["speakers"])} DER={record["der_collar250ms"]:.4f} '
            f'DER(c0)={record["der_collar0"]:.4f} JER={record["jer"]:.4f} '
            f'{record["elapsed_s"]}s'
        )

    native, pyannote = results.get('native', {}), results.get('pyannote', {})
    if 'der_collar250ms' in native and 'der_collar250ms' in pyannote:
        delta = native['der_collar250ms'] - pyannote['der_collar250ms']
        faster = pyannote['elapsed_s'] / max(native['elapsed_s'], 1e-9)
        print(f'\nnative - pyannote DER delta: {delta:+.4f}  |  native is {faster:.1f}x the speed')

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
