#!/usr/bin/env python3
"""Phase A.3 — Compute DER for every RTTM against the fp32/bs=16 reference.

In-container: same rule as vram-probe-diarization.py (fails outside Docker).
Reads RTTMs from docs/diarization-vram-profile/raw/rttm/ and emits
docs/diarization-vram-profile/accuracy.md with per-config DER + tier.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

from pyannote.core import Annotation, Segment
from pyannote.metrics.diarization import DiarizationErrorRate

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('der')

RTTM_RE = re.compile(
    r'^(?P<file>.+?)__cap-(?P<cap>[^_]+)__bs-(?P<bs>\d+)__mp-(?P<mp>on|off)__r(?P<r>\d+)\.rttm$'
)


def require_container() -> None:
    if Path('/.dockerenv').exists() or os.environ.get('OPENTRANSCRIBE_IN_CONTAINER') == '1':
        return
    sys.stderr.write('Refusing to run outside container. See A.0.0 post-mortem.\n')
    sys.exit(2)


def read_rttm(path: Path) -> Annotation:
    """Parse an RTTM file into a pyannote Annotation."""
    ann = Annotation(uri=path.stem)
    with open(path) as f:
        for line in f:
            parts = line.strip().split()
            if not parts or parts[0] != 'SPEAKER':
                continue
            start = float(parts[3])
            dur = float(parts[4])
            speaker = parts[7]
            ann[Segment(start, start + dur)] = speaker
    return ann


def classify(der: float, spk_match: bool) -> str:
    if not spk_match:
        return 'T3'
    if der <= 0.01:
        return 'T1'
    if der <= 0.03:
        return 'T2'
    return 'T3'


def main() -> int:
    require_container()
    rttm_dir = Path('/app/docs/diarization-vram-profile/raw/rttm')
    out_md = Path('/app/docs/diarization-vram-profile/accuracy.md')
    if not rttm_dir.exists():
        log.error(f'No RTTM dir: {rttm_dir}')
        return 1

    # Group by file
    by_file: dict[str, dict[tuple, Path]] = {}
    for p in sorted(rttm_dir.glob('*.rttm')):
        m = RTTM_RE.match(p.name)
        if not m:
            log.warning(f'Skip unparsable: {p.name}')
            continue
        f = m['file']
        key = (m['cap'], int(m['bs']), m['mp'], int(m['r']))
        by_file.setdefault(f, {})[key] = p

    der_metric = DiarizationErrorRate(collar=0.25, skip_overlap=False)
    results = []
    for file, configs in by_file.items():
        # Reference: prefer fp32 bs=16 unlimited r=0
        ref_key = ('unl', 16, 'off', 0)
        if ref_key not in configs:
            # Fall back to any fp32 unlimited
            cands = [k for k in configs if k[2] == 'off' and k[0] == 'unl']
            if not cands:
                log.warning(f'No fp32 unlimited reference for {file}; skipping')
                continue
            ref_key = sorted(cands, key=lambda k: abs(k[1] - 16))[0]
        ref = read_rttm(configs[ref_key])
        ref_spk = len(ref.labels())
        log.info(f'{file}: reference {ref_key} -> {ref_spk} speakers')
        for key, path in sorted(configs.items()):
            hyp = read_rttm(path)
            der = float(der_metric(ref, hyp))
            hyp_spk = len(hyp.labels())
            spk_match = hyp_spk == ref_spk
            tier = classify(der, spk_match)
            results.append(
                {
                    'file': file,
                    'cap': key[0],
                    'bs': key[1],
                    'mp': key[2],
                    'r': key[3],
                    'ref_spk': ref_spk,
                    'hyp_spk': hyp_spk,
                    'der': round(der, 4),
                    'tier': tier,
                    'is_reference': key == ref_key,
                }
            )

    # Write markdown
    lines = [
        '# Diarization Accuracy (Phase A.3)\n',
        'DER computed with `pyannote.metrics.DiarizationErrorRate(collar=0.25, skip_overlap=False)`.\n',
        'Reference for each file: fp32/bs=16/unlimited/r=0 (chosen per Phase A finding that bs=16 saturates throughput).\n',
        'Tiers: **T1** DER ≤ 1 %, **T2** DER ≤ 3 %, **T3** DER > 3 % or speaker-count mismatch.\n',
        '',
        '| file | cap | bs | mp | ref_spk | hyp_spk | DER | tier |',
        '|---|---|---:|---|---:|---:|---:|:---:|',
    ]
    for r in results:
        marker = ' *(reference)*' if r['is_reference'] else ''
        lines.append(
            f'| {r["file"]} | {r["cap"]} | {r["bs"]} | {r["mp"]} | {r["ref_spk"]} | '
            f'{r["hyp_spk"]} | {r["der"]:.4f} | **{r["tier"]}**{marker} |'
        )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text('\n'.join(lines) + '\n')
    log.info(f'Wrote {out_md} with {len(results)} rows')

    # Also dump JSON for downstream scripts
    (out_md.with_suffix('.json')).write_text(json.dumps(results, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
