#!/usr/bin/env python3
"""A/B a diar-server image's diarization accuracy across two OpenBLAS versions (issue #721).

**Why this exists.** ``diar-server`` is built ``openblas-system``: it has an ELF ``NEEDED``
entry for ``libopenblas.so.0`` and links whatever the *host image* provides. Upstream
validates on Ubuntu 24.04 (**0.3.26**); this repo's runtime stage is
``python:3.13-slim-trixie``, whose ``libopenblas0`` is **0.3.29+ds-3**. OpenBLAS 0.3.28/0.3.29
shipped an **arm64-only** GEMM->GEMV forwarding defect that moved upstream's AMI-16 DER from
13.8% to 48.7% — and ``diar-server verify-models`` passes throughout, reporting a plausible
speaker count while the diarization is wrong. A functional smoke test cannot see this; only a
DER measurement against hand-labelled ground truth can.

**What this measures.** The SAME ``diar-server`` binary, the SAME model export and the SAME
audio, run in two images that differ in their OpenBLAS. Both arms are pinned to
``DIAR_MODE=cpu`` because CPU is the bit-reproducible device (CUDA is not deterministic with
itself — see ``backend/app/transcription/CLAUDE.md``), so any output difference is attributable
to the library rather than to device nondeterminism.

**What it CANNOT do: answer the question for an architecture you are not running on.**
OpenBLAS selects kernels by *runtime CPU detection*, so a QEMU-emulated run measures the wrong
code path — upstream says so explicitly. Run this natively on each architecture you publish.
The script prints ``uname -m`` in its report for exactly that reason: a result is only ever a
claim about the machine that produced it.

Run it (defaults are the amd64 pairing this repo ships)::

    ./scripts/diar-openblas-der-ab.py \
        --audio  benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU/karpathy_10m.wav \
        --reference benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU/reference.rttm \
        --seconds 600

⚠️ **THE TRAP THAT PRODUCES A FAKE CATASTROPHE.** ``reference.rttm`` labels the WHOLE
66-minute source video (3,989 s); ``karpathy_10m.wav`` is only its first 10 minutes. Scoring
the clip against the uncropped reference makes ~85% of reference speech a miss and reports
**DER ~= 0.86 for every arm** — which reads exactly like the catastrophe this script hunts for
and is entirely an artefact. ``--seconds`` is checked against the decoded audio length and the
run is REFUSED on a mismatch; do not "fix" that by widening the tolerance.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path

#: Both arms run on CPU: it is the bit-reproducible device, so a difference is the library's.
BASE_ENV = {
    'DIAR_MODE': 'cpu',
    'DIAR_MODELS_DIR': '/models',
    'DIAR_BIND': '0.0.0.0:8701',
    'SPEAKRS_LAZY_SESSIONS': '1',
    'RUST_LOG': 'info',
    'DIAR_LOG_FORMAT': 'text',
    'HOME': '/tmp',  # noqa: S108 — the upstream image already sets HOME=/tmp for uid 10001
}

#: ``scripts/diar-native-der-parity.py``'s own gate. "Parity" between two arms is meaningless
#: if both are simply bad, so an absolute ceiling is checked as well as the delta.
DEFAULT_MAX_DER = 0.15


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)  # noqa: S603


def audio_seconds(path: Path) -> float:
    """Decoded length of a PCM WAV, used to refuse a mismatched ``--seconds``."""
    with wave.open(str(path), 'rb') as fh:
        return fh.getnframes() / float(fh.getframerate())


def observed_openblas(container: str) -> str:
    """The libopenblas actually MAPPED into the running process.

    Read from ``/proc/1/maps`` rather than from ``dpkg`` or a Dockerfile, because the only
    honest answer to "which OpenBLAS did this run use" is the one the loader resolved.
    """
    proc = _run(['docker', 'exec', container, 'cat', '/proc/1/maps'])
    paths = {
        tok
        for line in proc.stdout.splitlines()
        for tok in line.split()
        if tok.startswith('/') and 'libopenblas' in tok
    }
    return ', '.join(sorted(paths)) if paths else 'NONE MAPPED'


def start_arm(
    name: str,
    image: str,
    port: int,
    models: Path,
    audio_dir: Path,
    ld_library_path: str,
    extra_env: list[str],
) -> str:
    """Start one diar-server container and return its container name."""
    container = f'diar-blas-ab-{name}-{uuid.uuid4().hex[:8]}'
    cmd = ['docker', 'run', '-d', '--name', container, '--entrypoint', '/usr/local/bin/diar-server']
    for key, val in BASE_ENV.items():
        cmd += ['-e', f'{key}={val}']
    if ld_library_path:
        cmd += ['-e', f'LD_LIBRARY_PATH={ld_library_path}']
    for kv in extra_env:
        cmd += ['-e', kv]
    cmd += [
        '-v',
        f'{models}:/models:ro',
        '-v',
        f'{audio_dir}:/audio:ro',
        '-p',
        f'127.0.0.1:{port}:8701',
        image,
        'serve',
    ]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f'{name}: docker run failed: {proc.stderr.strip()}')
    return container


def wait_ready(port: int, container: str, timeout_s: int = 300) -> None:
    """Block until /readyz answers, or raise with the container's own logs attached."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        probe = _run(['curl', '-fsS', f'http://127.0.0.1:{port}/readyz'])
        if probe.returncode == 0:
            return
        time.sleep(2)
    logs = _run(['docker', 'logs', '--tail', '40', container])
    raise RuntimeError(f'{container}: never became ready.\n{logs.stdout}\n{logs.stderr}')


def diarize(port: int, wav: str) -> dict:
    """POST one /diarize job and return the parsed response."""
    payload = json.dumps({'wav_path': wav, 'file_id': 'openblas-ab', 'gender': False})
    proc = _run(
        [
            'curl',
            '-fsS',
            '-X',
            'POST',
            f'http://127.0.0.1:{port}/diarize',
            '-H',
            'Content-Type: application/json',
            '-d',
            payload,
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(f'/diarize failed: {proc.stderr.strip()}')
    return json.loads(proc.stdout)


def read_rttm(path: Path, seconds: float):
    """Parse an RTTM and crop it to the clip length — see the module docstring's trap note.

    Speaker labels in the committed reference contain SPACES ("Andrej Karpathy"), so the label
    is everything from field 8 on; taking ``parts[7]`` alone silently truncates it.
    """
    from pyannote.core import Annotation, Segment

    ann = Annotation(uri=path.stem)
    for line in path.open(encoding='utf-8'):
        parts = line.split()
        if not parts or parts[0] != 'SPEAKER':
            continue
        start, dur = float(parts[3]), float(parts[4])
        label = ' '.join(parts[7:]).replace('<NA>', '').strip() or parts[7]
        ann[Segment(start, start + dur)] = label
    return ann.crop(Segment(0.0, seconds), mode='intersection')


def to_annotation(segments: list[dict], uri: str):
    """Sidecar segments -> Annotation, dropping zero/negative-length spans."""
    from pyannote.core import Annotation, Segment

    ann = Annotation(uri=uri)
    for seg in segments:
        start, end = float(seg['start']), float(seg['end'])
        if end > start:
            ann[Segment(start, end)] = str(seg['speaker'])
    return ann


def score(reference, hypothesis) -> dict:
    """DER/JER under the same settings ``scripts/diar-native-der-parity.py`` uses."""
    from pyannote.metrics.diarization import DiarizationErrorRate, JaccardErrorRate

    return {
        'der_collar250ms': round(
            float(DiarizationErrorRate(collar=0.25, skip_overlap=True)(reference, hypothesis)), 4
        ),
        'der_collar0': round(
            float(DiarizationErrorRate(collar=0.0, skip_overlap=False)(reference, hypothesis)), 4
        ),
        'jer': round(
            float(JaccardErrorRate(collar=0.25, skip_overlap=True)(reference, hypothesis)), 4
        ),
    }


def run_arm(label: str, image: str, port: int, args, ld: str, extra_env: list[str]) -> dict:
    """Start an arm, record the OpenBLAS it really loaded, diarize, tear the container down."""
    container = start_arm(
        label,
        image,
        port,
        Path(args.models).resolve(),
        Path(args.audio).resolve().parent,
        ld,
        extra_env,
    )
    try:
        wait_ready(port, container)
        blas = observed_openblas(container)
        started = time.perf_counter()
        result = diarize(port, f'/audio/{Path(args.audio).name}')
        elapsed = round(time.perf_counter() - started, 1)
    finally:
        _run(['docker', 'rm', '-f', container])
    return {
        'label': label,
        'image': image,
        'openblas': blas,
        'elapsed_s': elapsed,
        'result': result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--audio', required=True, help='16 kHz mono WAV clip')
    parser.add_argument('--reference', required=True, help='hand-labelled RTTM')
    parser.add_argument(
        '--seconds',
        type=float,
        required=True,
        help='clip length; the reference is cropped to it (see the trap note)',
    )
    parser.add_argument(
        '--models', default='models/diar-native', help='diar-native model export mounted at /models'
    )
    parser.add_argument(
        '--image-a',
        default='opentranscribe-backend:latest',
        help='the image THIS repo ships (Debian trixie OpenBLAS)',
    )
    parser.add_argument(
        '--image-b',
        default='davidamacey/diar-native:0.3.1',
        help='upstream control (Ubuntu 24.04 OpenBLAS 0.3.26); "" to skip',
    )
    parser.add_argument(
        '--ld-a',
        default='/opt/diar-native/lib',
        help='LD_LIBRARY_PATH for image A (our images stage ORT libs there)',
    )
    parser.add_argument('--ld-b', default='', help='LD_LIBRARY_PATH for image B')
    parser.add_argument('--port-a', type=int, default=8791)
    parser.add_argument('--port-b', type=int, default=8792)
    parser.add_argument(
        '--max-der',
        type=float,
        default=DEFAULT_MAX_DER,
        help='fail if any arm exceeds this DER(collar=0.25)',
    )
    parser.add_argument('--json-out', default='')
    args = parser.parse_args()

    measured = audio_seconds(Path(args.audio))
    if abs(measured - args.seconds) > 5:
        print(
            f'REFUSING: audio is {measured:.1f}s but --seconds is {args.seconds}. Scoring a '
            'clip against a longer reference reports a fake catastrophe (~0.86 DER for every '
            'arm). Pass the real length.',
            file=sys.stderr,
        )
        return 2

    arch = _run(['uname', '-m']).stdout.strip()
    print(f'host architecture: {arch}   <-- this result is a claim about THIS arch only')
    print('QEMU cannot substitute: OpenBLAS picks kernels by runtime CPU detection.\n')

    reference = read_rttm(Path(args.reference), args.seconds)
    print(f'reference: {len(reference)} segments, speakers={sorted(reference.labels())}')
    print(f'crop:      0..{args.seconds}s\n')

    arms = [('A-shipped', args.image_a, args.port_a, args.ld_a)]
    if args.image_b:
        arms.append(('B-control', args.image_b, args.port_b, args.ld_b))

    records = []
    for label, image, port, ld in arms:
        rec = run_arm(label, image, port, args, ld, [])
        hyp = to_annotation(rec['result']['segments'], uri=label)
        rec['metrics'] = score(reference, hyp)
        rec['segments'] = len(rec['result']['segments'])
        rec['num_speakers'] = rec['result']['num_speakers']
        records.append(rec)
        print(f'{label:10s} {image}')
        print(f'  openblas loaded : {rec["openblas"]}')
        print(
            f'  segments={rec["segments"]:4d}  speakers={rec["num_speakers"]}  '
            f'DER={rec["metrics"]["der_collar250ms"]:.4f}  '
            f'DER(c0)={rec["metrics"]["der_collar0"]:.4f}  '
            f'JER={rec["metrics"]["jer"]:.4f}  ({rec["elapsed_s"]}s)\n'
        )

    # A harness that cannot report a catastrophe is not evidence that there isn't one.
    collapsed = to_annotation(
        [{**s, 'speaker': 'SPEAKER_00'} for s in records[0]['result']['segments']], uri='control'
    )
    ctrl = score(reference, collapsed)
    print(
        'CONTROL (all speech collapsed onto one speaker — what a clustering failure looks '
        f'like): DER={ctrl["der_collar250ms"]:.4f} JER={ctrl["jer"]:.4f}'
    )
    print('  ^ proves the metric DOES move when diarization is broken.\n')

    if len(records) == 2:
        same = records[0]['result']['segments'] == records[1]['result']['segments']
        print(f'A/B segment output identical: {same}')

    worst = max(r['metrics']['der_collar250ms'] for r in records)
    ok = worst <= args.max_der
    print(f'\nworst DER(0.25) = {worst:.4f}  (gate <= {args.max_der})')
    print(f'RESULT on {arch}: {"PASS" if ok else "FAIL"}')

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    'architecture': arch,
                    'arms': [{k: v for k, v in r.items() if k != 'result'} for r in records],
                    'control_collapsed': ctrl,
                    'max_der': args.max_der,
                    'worst_der': worst,
                    'pass': ok,
                },
                indent=2,
            )
        )
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
