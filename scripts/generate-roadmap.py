#!/usr/bin/env python3
"""Generate docs-site/src/data/roadmap.json from GitHub issues.

The roadmap is DERIVED from the issue tracker, never hand-maintained: epic labels
supply the grouping, milestones supply the release, and issue state supplies the
progress. A hand-written roadmap goes stale the first time an issue moves, and this
repo has already been bitten by hand-maintained tables of version facts.

This emits DATA, not markup. `docs-site/src/pages/roadmap.tsx` renders it, so the
presentation can change without touching the generator and vice versa. An earlier
version emitted a large Markdown page with a Mermaid diagram; it was accurate but
unreadable — a wall of tables and a diagram five screens tall.

Run after any milestone or epic-label change:

    python3 scripts/generate-roadmap.py
    cd docs-site && npm run build

Requires an authenticated `gh`. Exits 2 under ``--check`` if the tracker and the
checked-in data have drifted, which is what the CI job calls.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = 'attevon-llc/OpenTranscribe'
OUT = Path(__file__).resolve().parent.parent / 'docs-site' / 'src' / 'data' / 'roadmap.json'

#: Epic label suffix -> (display name, one-line scope).
#: A label missing here is surfaced in the output rather than silently dropped: an
#: unlabelled epic on the roadmap is indistinguishable from one nobody is working on.
EPICS: dict[str, tuple[str, str]] = {
    'rag-quality': ('RAG & Chat Quality', 'Retrieval, grounding, citations, summary search'),
    'frontend-ui': ('Frontend & UI', 'App surfaces, admin screens, and interface defects'),
    'search-infra': ('Search Infrastructure', 'Indexing, reindex correctness, index drift'),
    'compliance': ('Security & Compliance', 'Hardening, data protection, certification'),
    'platform-ops': ('Platform & Operations', 'Build, deploy, workers, GPU tuning, governance'),
    'document-ingestion': ('Document Ingestion', 'Documents as first-class library items'),
    'speaker-persona': ('Speaker Intelligence', 'Voiceprints, personas, cross-file identity'),
    'llm-providers': ('LLM Providers', 'Provider integrations and configuration'),
    'native-diarizer': ('Native Diarizer', 'Rust/ONNX diarization; retiring PyTorch/PyAnnote'),
    'meeting-capture': ('Meeting Capture', 'Meeting ingestion, calendar, pre-meeting briefs'),
    'public-demo': ('Public Demo', 'Read-only hosted demo deployment'),
    'asr-engines': ('ASR Engines', 'Alternative and native transcription engines'),
    'desktop': ('Desktop App', 'Standalone cross-platform application'),
}

#: Release -> (headline, one-sentence summary). Keep the summary to a single
#: sentence: the prior art (Immich, GitHub's own roadmap) runs 10-15 words per item,
#: and the long-form rationale belongs in the linked issues, not here.
RELEASES: dict[str, tuple[str, str]] = {
    'v0.5.0': (
        'Deployment and release hardening',
        'Release tooling, deployment shapes, and the security and correctness work '
        'that came out of a full rehearsal.',
    ),
    'v0.6.0': (
        'Answer quality and interface polish',
        'Make what already ships correct — grounded answers, searchable summaries, '
        'an interface pass, and the fixes that affect running deployments today.',
    ),
    'v0.7.0': (
        'Documents, speakers, and providers',
        'Widen the library beyond audio, deepen cross-file speaker identity, and add '
        'provider breadth.',
    ),
    'v0.8.0': (
        'Native diarization',
        'Retire the in-process PyTorch diarizer for the native Rust/ONNX engine, '
        'including the voiceprint migration it depends on.',
    ),
    'v0.9.0': (
        'Meetings and extensibility',
        'Bring meetings in automatically and open the pipeline to external tooling.',
    ),
    'v1.0.0': (
        'Platform maturity',
        'Alternative transcription engines, a desktop application, live transcription, '
        'and formal compliance validation.',
    ),
}

RELEASE_ORDER = list(RELEASES)

#: now/next/later rather than dates. Immich's own community asked for exactly this
#: (immich-app/immich discussion #27924): it says what is being worked on without
#: committing to a date the project cannot honour.
STAGES = ['now', 'next', 'later']


def released_versions() -> set[str]:
    """Versions with a git tag.

    A milestone whose issues are all closed is COMPLETE, not SHIPPED — this repo
    currently has `VERSION` at v0.5.0 with no v0.5.0 tag, so inferring "shipped" from
    issue state alone would publish a release that does not exist. The tag is the only
    honest signal, so absence of git degrades to "complete", never to "shipped".
    """
    try:
        out = subprocess.run(
            ['git', 'tag', '--list', 'v*'], capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def gh_issues() -> list[dict]:
    """Every issue carrying an ``epic:`` label, open or closed."""
    raw = subprocess.run(
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
    for issue in json.loads(raw):
        epics = [
            label['name'].removeprefix('epic:')
            for label in issue['labels']
            if label['name'].startswith('epic:')
        ]
        if epics:
            issue['epic'] = epics[0]
            issues.append(issue)
    return issues


def _release_sort_key(release: str) -> tuple:
    if release in RELEASE_ORDER:
        return (0, RELEASE_ORDER.index(release))
    return (1, release)


def _stage(release: str, total: int, closed: int, tagged: set[str], planned: dict) -> str:
    if release in tagged:
        return 'shipped'
    if total and closed == total:
        return 'complete'
    return planned.get(release, '')


def build_data(issues: list[dict]) -> dict:
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for issue in issues:
        milestone = (issue.get('milestone') or {}).get('title')
        if milestone is None:
            # Unscheduled work belongs on the roadmap only while it is still OPEN.
            # A CLOSED issue with no milestone is finished work that was never
            # attributed to a release — history, not plan. Including it rendered a
            # "Backlog — 17 of 18 complete" entry, which is nonsense: a backlog
            # cannot be complete.
            if issue['state'] == 'CLOSED':
                continue
            milestone = 'unscheduled'
        grouped[milestone][issue['epic']].append(issue)

    tagged = released_versions()

    # now/next/later is assigned over releases with work REMAINING. Counting a
    # finished release would push the active one to "next" and leave nothing marked
    # as in progress.
    def _remaining(release: str) -> bool:
        group = grouped[release]
        issues_in = [i for area in group.values() for i in area]
        return release not in tagged and any(i['state'] == 'OPEN' for i in issues_in)

    scheduled = [
        r for r in sorted(grouped, key=_release_sort_key) if r in RELEASES and _remaining(r)
    ]
    stage_for: dict[str, str] = {}
    for index, release in enumerate(scheduled):
        stage_for[release] = STAGES[index] if index < len(STAGES) else STAGES[-1]
    releases = []
    for release in sorted(grouped, key=_release_sort_key):
        headline, summary = RELEASES.get(release, ('', ''))
        areas = []
        total = closed_total = 0
        for epic in sorted(grouped[release], key=lambda e: EPICS.get(e, (e,))[0]):
            group = sorted(grouped[release][epic], key=lambda i: i['number'])
            closed = sum(1 for i in group if i['state'] == 'CLOSED')
            total += len(group)
            closed_total += closed
            name, scope = EPICS.get(epic, (epic, ''))
            areas.append(
                {
                    'key': epic,
                    'name': name,
                    'scope': scope,
                    'known': epic in EPICS,
                    'total': len(group),
                    'closed': closed,
                    'issues': [
                        {
                            'number': i['number'],
                            'title': i['title'],
                            'done': i['state'] == 'CLOSED',
                        }
                        for i in group
                    ],
                }
            )
        releases.append(
            {
                'version': release,
                'headline': headline,
                'summary': summary,
                'stage': _stage(release, total, closed_total, tagged, stage_for),
                'released': release in tagged,
                'scheduled': release in RELEASES,
                'total': total,
                'closed': closed_total,
                'areas': areas,
            }
        )

    return {
        'repo': REPO,
        'project': 'https://github.com/orgs/attevon-llc/projects/1',
        'releases': releases,
        'unmappedEpics': sorted({i['epic'] for i in issues} - set(EPICS)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--check',
        action='store_true',
        help='exit 2 if the generated data differs from what is checked in',
    )
    args = parser.parse_args()

    data = build_data(gh_issues())
    rendered = json.dumps(data, indent=2, ensure_ascii=False) + '\n'

    if args.check:
        current = OUT.read_text() if OUT.exists() else ''
        if current != rendered:
            print(
                f'{OUT} is out of date — run: python3 scripts/generate-roadmap.py', file=sys.stderr
            )
            return 2
        print(f'{OUT} is up to date')
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(rendered)
    counts = ', '.join(f'{r["version"]} {r["closed"]}/{r["total"]}' for r in data['releases'])
    print(f'wrote {OUT}\n  {counts}')
    if data['unmappedEpics']:
        print(f'  WARNING unmapped epic labels: {", ".join(data["unmappedEpics"])}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
