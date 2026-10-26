#!/usr/bin/env python3
"""Time the DOCX / PDF transcript renderers on a synthetic recording (issue #1278).

MANUAL ONLY. It is deliberately not a test: a wall-clock budget in the suite is a flaky gate
(``backend/tests/CLAUDE.md``, "fixed wall-clock budget"), and a number from a loaded host
proves nothing. Run it on a quiet host, ideally inside the built backend image where the real
fonts live at ``DOCUMENT_FONT_DIR``:

    docker compose exec backend python /app/scripts/bench-transcript-document.py
    backend/venv/bin/python scripts/bench-transcript-document.py --font-dir /path/to/fonts

Each run renders ``--hours`` of speech (900 segments of 10 words per hour, so 4 h is 3,600
segments) in one language and prints wall time, peak RSS, pages and output size. Latin should
take about 5 s per four hours, because shaping is switched on per line only for scripts that
need it; Arabic is the slow case (about a minute per four hours, all of it shaping).
"""

from __future__ import annotations

import argparse
import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
sys.path.insert(0, str(BACKEND))

WORDS = {
    'en': [
        'the',
        'quarterly',
        'numbers',
        'look',
        'fine',
        'and',
        'we',
        'should',
        'review',
        'the',
        'budget',
        'again',
        'soon',
    ],
    'ar': [
        'مرحبا',
        'بالعالم',
        'هذا',
        'اختبار',
        'للنص',
        'العربي',
        'الطويل',
        'في',
        'تقرير',
        'الاجتماع',
    ],
    'th': ['สวัสดีครับ', 'ขอบคุณมากครับ', 'วันนี้อากาศดีมาก', 'เราไปกินข้าวกันไหม'],
    'ja': ['今日は会議の結果について報告します', '来月の予算を確認しましょう'],
    'zh': ['今天我们讨论第四季度的预算', '请大家确认下个月的计划'],
}
LANGUAGE_CODE = {'en': 'en', 'ar': 'ar', 'th': 'th', 'ja': 'ja', 'zh': 'zh'}


def build_blocks(language: str, hours: float):
    from app.services.transcript_document.model import TurnBlock
    from app.services.transcript_document.scripts import base_direction

    words = WORDS[language]
    separator = '' if language in ('ja', 'zh') else ' '
    blocks = []
    for index in range(int(900 * hours)):
        text = separator.join(words[(index + step) % len(words)] for step in range(10))
        seconds = index * 4
        header = (
            f'[{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}] S{index % 3}:'
        )
        blocks.append(TurnBlock(header, text, base_direction(text)))
    return blocks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--hours', type=float, default=4.0)
    parser.add_argument('--languages', default='en,ar', help='comma list of: ' + ','.join(WORDS))
    parser.add_argument('--formats', default='pdf,docx')
    parser.add_argument('--font-dir', type=Path, default=None, help='default: DOCUMENT_FONT_DIR')
    args = parser.parse_args()

    from app.services.transcript_document.docx_renderer import render_docx
    from app.services.transcript_document.model import (
        DocumentLabels,
        DocumentMeta,
        DocumentOptions,
        PageSpec,
    )
    from app.services.transcript_document.pdf_renderer import render_pdf

    options = DocumentOptions()
    spec = PageSpec.named(options.page_size)
    labels = DocumentLabels()
    for language in args.languages.split(','):
        blocks = build_blocks(language, args.hours)
        meta = DocumentMeta(
            title='Benchmark recording',
            exported_at=datetime.now(UTC),
            redacted=False,
            language=LANGUAGE_CODE[language],
        )
        for fmt in args.formats.split(','):
            started = time.perf_counter()
            if fmt == 'pdf':
                data = render_pdf(blocks, spec, options, labels, meta, font_dir=args.font_dir)
            else:
                data = render_docx(blocks, spec, options, labels, meta)
            elapsed = time.perf_counter() - started
            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            print(
                f'{language} {fmt:4} {args.hours:g}h segments={len(blocks)} '
                f'wall={elapsed:6.1f}s peak_rss={rss_mb:6.0f}MB size={len(data) / 1024:8.0f}KB'
            )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
