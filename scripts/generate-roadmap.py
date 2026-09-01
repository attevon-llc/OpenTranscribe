#!/usr/bin/env python3
"""Generate docs-site/docs/roadmap.md from GitHub issues.

The roadmap is DERIVED from the issue tracker, never hand-maintained: epic labels
supply the grouping, milestones supply the version, and issue state supplies the
progress. A hand-written roadmap goes stale the first time an issue moves, and this
repo has already been bitten by hand-maintained tables of version facts.

Run after any milestone or epic-label change:

    python3 scripts/generate-roadmap.py
    cd docs-site && npm run build     # verify the Mermaid renders

Requires an authenticated `gh`. Exits 2 if the tracker and the checked-in page have
drifted (``--check``), which is what a CI job would call.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO = 'attevon-llc/OpenTranscribe'
PROJECT_URL = 'https://github.com/orgs/attevon-llc/projects/1'
OUT = Path(__file__).resolve().parent.parent / 'docs-site' / 'docs' / 'roadmap.md'

#: Epic label suffix -> (display name, one-line scope description).
#: A label missing here is reported rather than silently dropped: an unlabelled
#: epic on the roadmap is indistinguishable from one nobody is working on.
EPICS: dict[str, tuple[str, str]] = {
    'rag-quality': ('RAG & Chat Quality', 'Retrieval, grounding, citations, summary search'),
    'frontend-ui': ('Frontend & UI', 'SPA surfaces, admin screens, and UI defect fixes'),
    'search-infra': ('Search Infrastructure', 'OpenSearch indexing, reindex correctness, drift'),
    'compliance': ('Security & Compliance', 'Hardening, data protection, certification work'),
    'platform-ops': ('Platform & Operations', 'Build, deploy, workers, GPU tuning, governance'),
    'document-ingestion': (
        'Document Ingestion',
        'Non-audio documents as first-class library items',
    ),
    'speaker-persona': (
        'Speaker Intelligence',
        'Voiceprint matching, personas, cross-file identity',
    ),
    'llm-providers': ('LLM Providers', 'Provider integrations and provider-config UX'),
    'native-diarizer': ('Native Diarizer', 'Rust/ONNX diarization; retiring PyTorch/PyAnnote'),
    'meeting-capture': ('Meeting Capture', 'Recall.ai ingestion, calendar, pre-meeting briefs'),
    'public-demo': ('Public Demo', 'Read-only hosted demo deployment'),
    'asr-engines': ('ASR Engines', 'Alternative and native transcription engines'),
    'desktop': ('Desktop App', 'Standalone cross-platform application'),
}

#: Version -> (headline theme, prose). Versions absent from the tracker are skipped.
THEMES: dict[str, tuple[str, str]] = {
    'v0.6.0': (
        'Answer quality and interface polish',
        'Makes what already ships correct: RAG answers that cite what they used, '
        'searchable summaries, an interface pass, and the security and data-integrity '
        'fixes that affect running deployments today.',
    ),
    'v0.7.0': (
        'Documents, speakers, and provider breadth',
        'Widens the library beyond audio, deepens cross-file speaker identity, and '
        'adds the LLM providers and pipeline efficiency work that the quality release '
        'depends on but does not block.',
    ),
    'v0.8.0': (
        'Native diarization',
        'Retires the in-process PyTorch/PyAnnote diarizer in favour of the native '
        'Rust/ONNX engine, including the voiceprint migration and the deployment and '
        'test coverage that has to exist first.',
    ),
    'v0.9.0': (
        'Meetings and extensibility',
        'Brings meetings in automatically — calendar-aware capture and briefs — and '
        'opens the pipeline to external tooling.',
    ),
    'v1.0.0': (
        'Platform maturity',
        'Alternative ASR engines, a standalone desktop application, live transcription, '
        'and formal compliance validation.',
    ),
}

VERSION_ORDER = list(THEMES)


def gh_issues() -> list[dict]:
    """All issues (any state) carrying an ``epic:`` label."""
    out = subprocess.run(
        [
            'gh',
            'issue',
            'list',
            '--repo',
            REPO,
            '--state',
            'all',
            '--limit',
            '1000',
            '--json',
            'number,title,state,labels,milestone',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    issues = []
    for issue in json.loads(out):
        epics = [
            label['name'].removeprefix('epic:')
            for label in issue['labels']
            if label['name'].startswith('epic:')
        ]
        if epics:
            issue['epic'] = epics[0]
            issues.append(issue)
    return issues


def _version_sort_key(version: str) -> tuple:
    if version in VERSION_ORDER:
        return (0, VERSION_ORDER.index(version))
    return (1, version)


def _node_id(version: str, epic: str) -> str:
    return f'{version.replace(".", "")}_{epic.replace("-", "")}'


def build_mermaid(grouped: dict[str, dict[str, list[dict]]]) -> str:
    """One node per release, listing its epics and their progress.

    Two shapes were tried and rejected. One node per *issue* is a wall at ~75 issues.
    One node per *epic* inside a per-version ``subgraph`` renders as a single tall
    column, because Mermaid ignores ``direction LR`` nested inside a ``graph TD``
    subgraph — verified in the built page, not assumed. Folding the epic list into
    the release node keeps the diagram five boxes tall no matter how many epics or
    issues exist, and the tables below carry the per-issue detail.
    """
    lines = [
        '```mermaid',
        'graph TD',
        '  classDef done fill:#1a7f37,stroke:#116329,color:#ffffff,text-align:left;',
        '  classDef active fill:#0969da,stroke:#0550ae,color:#ffffff,text-align:left;',
        '  classDef planned fill:#f6f8fa,stroke:#8c959f,color:#1f2328,text-align:left;',
    ]
    versions = [v for v in sorted(grouped, key=_version_sort_key) if v != '(unscheduled)']
    for version in versions:
        theme = THEMES.get(version, ('', ''))[0]
        rows = []
        total = closed_total = 0
        for epic in sorted(grouped[version], key=lambda e: EPICS.get(e, (e,))[0]):
            issues = grouped[version][epic]
            closed = sum(1 for i in issues if i['state'] == 'CLOSED')
            total += len(issues)
            closed_total += closed
            name = EPICS.get(epic, (epic, ''))[0]
            rows.append(f'{name} · {closed}/{len(issues)}')
        cls = 'done' if closed_total == total else ('active' if closed_total else 'planned')
        head = f'<b>{version}</b> — {theme}' if theme else f'<b>{version}</b>'
        body = '<br/>'.join(rows)
        lines.append(
            f'  {version.replace(".", "_")}["{head}<br/><i>{closed_total}/{total} issues '
            f'complete</i><br/><br/>{body}"]:::{cls}'
        )
    for earlier, later in zip(versions, versions[1:], strict=False):
        lines.append(f'  {earlier.replace(".", "_")} --> {later.replace(".", "_")}')
    lines.append('```')
    return '\n'.join(lines)


def build_page(issues: list[dict]) -> str:
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for issue in issues:
        milestone = (issue.get('milestone') or {}).get('title') or '(unscheduled)'
        grouped[milestone][issue['epic']].append(issue)

    unknown = sorted({i['epic'] for i in issues} - set(EPICS))
    generated = datetime.now(UTC).strftime('%Y-%m-%d')

    parts = [
        '---',
        'id: roadmap',
        'title: Roadmap',
        'sidebar_label: Roadmap',
        'description: Planned OpenTranscribe releases, the themes behind them, and live progress.',
        # Without this the right-hand TOC lists all ~30 epic headings and becomes a
        # second, noisier copy of the page.
        'toc_max_heading_level: 2',
        '---',
        '',
        '# Roadmap',
        '',
        'Every item below is a real GitHub issue. **This page is generated from the issue '
        'tracker** — it cannot describe work that is not tracked, and it cannot go stale '
        'while an issue moves.',
        '',
        f'- **Source of truth:** the [project board]({PROJECT_URL}) and the issues it contains',
        '- **Grouping:** each issue carries exactly one `epic:*` label',
        "- **Version:** the issue's GitHub milestone",
        '- **Progress:** open vs. closed issue counts, not estimates',
        '',
        # Docusaurus v3 takes the admonition title in BRACKETS. The v2 space-separated
        # form (`:::note Title`) renders as literal text — verified in the built page.
        ':::note[Dates are sequence, not commitment]',
        '',
        'Versions are ordered by dependency. A later version is not scheduled for a date — '
        'it is blocked on the one before it. Scope moves between versions as work is '
        'understood; that is expected, not drift.',
        '',
        ':::',
        '',
        '## Release flow',
        '',
        build_mermaid(grouped),
        '',
    ]

    for version in sorted(grouped, key=_version_sort_key):
        theme, prose = THEMES.get(version, ('', ''))
        epics = grouped[version]
        all_issues = [i for group in epics.values() for i in group]
        closed = sum(1 for i in all_issues if i['state'] == 'CLOSED')

        heading = f'## {version}' + (f' — {theme}' if theme else '')
        parts += [heading, '']
        if prose:
            parts += [prose, '']
        parts += [f'**{closed} of {len(all_issues)} issues complete.**', '']

        for epic in sorted(epics, key=lambda e: EPICS.get(e, (e,))[0]):
            name, scope = EPICS.get(epic, (epic, ''))
            group = sorted(epics[epic], key=lambda i: i['number'])
            done = sum(1 for i in group if i['state'] == 'CLOSED')
            parts += [f'### {name} · {done}/{len(group)}', '']
            if scope:
                parts += [f'_{scope}_', '']
            parts += ['| | Issue |', '|---|---|']
            for issue in group:
                mark = '✅' if issue['state'] == 'CLOSED' else '◻️'
                title = issue['title'].replace('|', '\\|')
                parts.append(
                    f'| {mark} | [#{issue["number"]}]'
                    f'(https://github.com/{REPO}/issues/{issue["number"]}) {title} |'
                )
            parts += [
                '',
                f'Browse: [`epic:{epic}`](https://github.com/{REPO}/labels/epic%3A{epic})',
                '',
            ]

    parts += [
        '## How this page is maintained',
        '',
        'Regenerate after any milestone or epic-label change:',
        '',
        '```bash',
        'python3 scripts/generate-roadmap.py',
        '```',
        '',
        '`--check` exits non-zero when the tracker and this page disagree, so a CI job can '
        'fail on drift rather than letting the published roadmap quietly diverge from the '
        'board.',
        '',
        'To move an item, change its **milestone** (version) or its **`epic:*` label** '
        '(grouping) on the issue and regenerate. Editing this file by hand is pointless — '
        'the next run overwrites it.',
        '',
    ]

    if unknown:
        parts += [
            ':::warning Unmapped epics',
            'These epic labels have no entry in `EPICS` in `scripts/generate-roadmap.py`, so '
            'they render with a raw label name and no scope description: '
            + ', '.join(f'`epic:{e}`' for e in unknown)
            + '.',
            ':::',
            '',
        ]

    # MDX v3 rejects HTML comments (`<!-- -->`) outright — the marker has to be a JSX
    # expression comment or the docs build fails on this line.
    parts.append(f'{{/* Generated {generated} by scripts/generate-roadmap.py. Do not edit. */}}')
    return '\n'.join(parts) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--check',
        action='store_true',
        help='exit 2 if the generated page differs from what is checked in',
    )
    args = parser.parse_args()

    page = build_page(gh_issues())

    if args.check:
        current = OUT.read_text() if OUT.exists() else ''

        # The trailing generated-on stamp changes every run; compare the body only.
        def strip(text: str) -> str:
            return '\n'.join(
                line for line in text.splitlines() if not line.startswith('{/* Generated ')
            )

        if strip(current) != strip(page):
            print(
                f'{OUT} is out of date — run: python3 scripts/generate-roadmap.py', file=sys.stderr
            )
            return 2
        print(f'{OUT} is up to date')
        return 0

    OUT.write_text(page)
    print(f'wrote {OUT} ({len(page.splitlines())} lines)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
