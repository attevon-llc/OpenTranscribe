#!/usr/bin/env python3
"""Measure what issue #661's E2 path handoff actually saves, as a controlled A/B.

#661 asks for ``user_perceived_duration_ms`` before E0 and after each of E0->E5. That form
needs two builds of the whole stack; this measures the same change more precisely and with
far less confounding, because **both arms run in one process, on one GPU, over one decoded
waveform, against one sidecar** — the only thing that varies is the handoff:

    arm A  wav_path=<WAV on the shared volume>  -> path handoff        (post-E2)
    arm B  wav_path=None                        -> _post_own_copy      (pre-E2 behaviour)

``NativeSpeakerDiarizer.diarize`` decides with
``reused = wav_path and _path_is_on_shared_volume(wav_path) and isfile(wav_path)``, so arm B
is *structurally* the old path: ``None`` cannot satisfy the first clause. That is why the arms
cannot silently converge — unlike a flag, which could be read wrong.

⚠️ **The one way this measures nothing** is arm A quietly failing the shared-volume predicate
and taking the own-copy path too, leaving two identical arms and a delta of ~0 that reads like
"the optimisation does nothing". That is checked and made FATAL before any timing runs, rather
than inferred from the numbers afterwards.

Run it in the GPU worker, which owns the models, the GPU and the sidecar URL::

    W=opentranscribe-celery-worker
    docker cp scripts/diar-native-handoff-ab.py $W:/tmp/
    docker cp benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU/karpathy_10m.wav $W:/tmp/
    docker exec $W python3 /tmp/diar-native-handoff-ab.py --audio /tmp/karpathy_10m.wav

Arms ALTERNATE (ABAB…), never AAA then BBB: the GPU clocks down as it heats and the sidecar's
ORT arena warms, so a blocked design charges all of one arm's drift to the other's difference.
Reported as median and p95 over n reps, with a discarded warm-up — a mean over 3 runs on a
shared box is not a measurement.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path


def _load_audio(path: str):
    """Decode to 16 kHz mono float32 — the shape the whole engine passes around.

    ``scipy.io.wavfile`` deliberately, because that is what the app's own
    ``engine/audio_loader.py`` uses. soundfile and librosa are not installed in the worker,
    and adding a decoder the pipeline does not use would mean the measured arms were fed by a
    different code path than production.
    """
    import numpy as np
    import scipy.io.wavfile as wavfile

    sr, data = wavfile.read(path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    # int16 -> float32 in [-1, 1], matching write_wav_to_shared_volume's inverse scaling.
    if data.dtype.kind == 'i':
        data = data.astype('float32') / float(np.iinfo(data.dtype).max)
    else:
        data = data.astype('float32')
    if sr != 16000:
        import scipy.signal as sps

        data = sps.resample_poly(data, 16000, sr).astype('float32')
    return np.ascontiguousarray(data, dtype='float32')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--audio', default='/tmp/karpathy_10m.wav')  # noqa: S108
    ap.add_argument('--reps', type=int, default=5, help='timed reps PER ARM')
    ap.add_argument('--json-out', default='')
    args = ap.parse_args()

    sys.path.insert(0, '/app')

    # resolve_engine_shared_volume_path() rather than a config attribute: it is the single
    # reader of ENGINE_SHARED_VOLUME_PATH (app/core/constants.py) and the exact value
    # diarizer_native._ENGINE_SHARED_DIR is built from, so the WAV is guaranteed to be written
    # where _path_is_on_shared_volume looks. Reading it from anywhere else reintroduces the
    # two-sources-of-truth bug the resolver exists to remove.
    from app.core.constants import resolve_engine_shared_volume_path
    from app.transcription.config import TranscriptionConfig
    from app.transcription.diarizer_native import (
        NativeSpeakerDiarizer,
        _path_is_on_shared_volume,
        sidecar_ready,
    )
    from app.transcription.engine.audio_loader import write_wav_to_shared_volume

    if not sidecar_ready():
        print(
            'FATAL: sidecar is not ready — both arms would fall back to PyAnnote and the '
            'comparison would measure the fallback, not the handoff.'
        )
        return 3

    audio = _load_audio(args.audio)
    duration_s = len(audio) / 16000.0
    config = TranscriptionConfig.from_environment()
    shared_dir = resolve_engine_shared_volume_path()

    wav = write_wav_to_shared_volume(audio, shared_dir, 'handoff-ab')
    if not wav or not os.path.isfile(wav):
        print(f'FATAL: could not write a WAV to the shared volume ({shared_dir!r})')
        return 3

    # THE GUARD. If this is False, arm A takes _post_own_copy exactly like arm B, both arms
    # are the same code, and the delta is ~0 for a reason that has nothing to do with #661.
    if not _path_is_on_shared_volume(wav):
        print(
            f"FATAL: {wav} is not on the shared volume by the engine's own predicate, so arm A "
            f'would take the own-copy path too and this would compare a path to itself. '
            f'shared_dir={shared_dir!r}'
        )
        return 3

    diarizer = NativeSpeakerDiarizer(config)
    diarizer.load_model()

    print(f'audio        : {args.audio} ({duration_s:.1f}s)')
    print(f'shared wav   : {wav}')
    print(f'reps per arm : {args.reps} (alternating, 1 warm-up per arm discarded)\n')

    timings: dict[str, list[float]] = {'reused': [], 'own_copy': []}
    segments: dict[str, list[int]] = {'reused': [], 'own_copy': []}

    # Warm-up, discarded: the first call pays ORT session init and CUDA context setup, which
    # would otherwise land entirely on whichever arm happens to run first.
    for arm_wav in (wav, None):
        diarizer.diarize(audio, arm_wav)

    for rep in range(args.reps):
        for arm, arm_wav in (('reused', wav), ('own_copy', None)):
            started = time.perf_counter()
            result, _overlap, _emb = diarizer.diarize(audio, arm_wav)
            elapsed = time.perf_counter() - started
            timings[arm].append(elapsed)
            segments[arm].append(len(result))
            print(f'  rep {rep + 1} {arm:9s} {elapsed:7.3f}s  ({len(result)} segments)')

    def stats(xs: list[float]) -> dict:
        ordered = sorted(xs)
        return {
            'n': len(xs),
            'median_s': round(statistics.median(ordered), 3),
            'p95_s': round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
            'min_s': round(ordered[0], 3),
            'max_s': round(ordered[-1], 3),
        }

    a, b = stats(timings['reused']), stats(timings['own_copy'])
    saved = b['median_s'] - a['median_s']
    pct = (saved / b['median_s'] * 100.0) if b['median_s'] else 0.0

    print('\n  arm                median     p95      min      max')
    print(
        f'  reused (post-E2)  {a["median_s"]:7.3f}  {a["p95_s"]:7.3f}  {a["min_s"]:7.3f}  {a["max_s"]:7.3f}'
    )
    print(
        f'  own_copy (pre-E2) {b["median_s"]:7.3f}  {b["p95_s"]:7.3f}  {b["min_s"]:7.3f}  {b["max_s"]:7.3f}'
    )
    print(
        f'\n  E2 saves {saved:.3f}s per diarization ({pct:.1f}%) on a {duration_s:.0f}s recording'
    )

    # Same segment count in both arms or the arms are not comparable — a different result
    # means something other than the handoff changed.
    if set(segments['reused']) != set(segments['own_copy']):
        print(
            f'\n  ⚠️  segment counts differ between arms: {set(segments["reused"])} vs '
            f'{set(segments["own_copy"])} — the arms are not equivalent, treat the delta '
            f'as unattributed'
        )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    'audio': args.audio,
                    'duration_s': round(duration_s, 1),
                    'reused': a,
                    'own_copy': b,
                    'saved_s': round(saved, 3),
                    'saved_pct': round(pct, 1),
                    'segments': {k: sorted(set(v)) for k, v in segments.items()},
                },
                indent=2,
            ),
            encoding='utf-8',
        )
        print(f'\n  wrote {args.json_out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
