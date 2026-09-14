#!/usr/bin/env python3
"""Build the GitHub Release body from a CHANGELOG section, within GitHub's size cap.

`95-finish.sh` publishes the `## [X.Y.Z]` section of `CHANGELOG.md` as the release body.
v0.5.0's section is 2,917 lines / ~350 KB, which exceeds GitHub's ~125,000-character limit,
so the body has to be shortened. Shortening it naively is what this module exists to avoid.

THE BUG THIS WAS WRITTEN FOR (2026-09-14, caught on the real v0.5.0 release). The first fix
trimmed the body to its `### Overview` alone. That Overview says, twice:

    **If you redistribute or host this software, read [Upgrade Notes](#upgrade-notes).**
    **This release contains breaking changes** — see [Upgrade Notes](#upgrade-notes) ...

Once the body is Overview-only, `### Upgrade Notes` is no longer in it, so GitHub generates
no `#upgrade-notes` anchor and both links resolve to nothing. Verified against the published
release: the body contained exactly one heading (`### Overview`) and one referenced anchor
(`#upgrade-notes`). The reader is pointed at the AGPL §13 obligations and the breaking-change
list by a link that goes nowhere — a worse outcome than shipping no link at all, because a
dead link looks like a live one until clicked.

TWO RULES FOLLOW, and the second is the general one:

1. `### Upgrade Notes` is KEPT, not just `### Overview`. It is the section a reader must not
   miss (v0.5.0's opens "the backend will REFUSE TO START if your .env lacks production
   secrets"), and Overview + Upgrade Notes is ~26 KB — comfortably inside the cap.
2. Any anchor still referenced with no surviving heading is REWRITTEN to an absolute
   CHANGELOG URL. Rule 1 alone fixes the case we hit; rule 2 is what stops the next one,
   because a future Overview may link to any section at all.

⚠️ `anchor_slug` FAILS TOWARD REWRITING. It approximates GitHub's heading-slug algorithm, and
an approximation can be wrong in two directions. Claiming an anchor is LIVE when it is dead
reintroduces the exact bug; rewriting one that would have worked in-page costs a reader
nothing (the absolute link goes to the same content). So when the slug is uncertain, treat
the anchor as dangling.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

#: GitHub's documented cap on a release body. The default here sits below it so that a body
#: at exactly the boundary is still accepted rather than 422'd on a rounding difference.
GITHUB_BODY_CAP = 125_000
DEFAULT_CAP = 120_000

#: Sections kept, in this order, when the whole section does not fit. Order is the reading
#: order a release page wants, NOT the order they appear in the changelog.
KEPT_HEADINGS = ('Overview', 'Upgrade Notes')

_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*$', re.M)
_ANCHOR_REF_RE = re.compile(r'\]\(#([A-Za-z0-9_-]+)\)')


@dataclass(frozen=True)
class Section:
    level: int
    title: str
    body: str

    @property
    def text(self) -> str:
        return f'{"#" * self.level} {self.title}\n{self.body}'


def anchor_slug(heading: str) -> str:
    """Approximate the anchor GitHub generates for a heading.

    Mirrors `github-slugger`: lowercase, drop anything that is not a word character / space /
    hyphen (so punctuation and emoji vanish), then map each SPACE to a hyphen.

    ⚠️ Runs are deliberately NOT collapsed, and underscores are deliberately kept. GitHub
    renders `### Fixed — testing` as `#fixed--testing` — the em-dash is removed and BOTH
    surrounding spaces become hyphens. Collapsing them here produced `#fixed-testing`, which
    never matches the real anchor, so every such link was treated as dangling and needlessly
    rewritten. That is the safe direction (see the module docstring) but it is still wrong,
    and a reader loses the in-page jump for no reason.
    """
    slug = heading.strip().lower()
    slug = re.sub(r'[^\w\s-]', '', slug, flags=re.UNICODE)
    # Normalise exotic whitespace to a plain space first; a heading is a single line, so this
    # only affects tabs and non-breaking spaces.
    slug = re.sub(r'\s', ' ', slug)
    return slug.replace(' ', '-')


def split_sections(text: str) -> list[Section]:
    """Split a changelog entry into its headed sections, in document order.

    Text before the first heading is returned as a level-0 `Section` with an empty title, so
    a preamble is never silently dropped by a caller that iterates sections.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [Section(0, '', text)]

    sections: list[Section] = []
    preamble = text[: matches[0].start()]
    if preamble.strip():
        sections.append(Section(0, '', preamble))

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(Section(len(m.group(1)), m.group(2), text[m.end() : end]))
    return sections


def live_anchors(text: str) -> set[str]:
    """Every anchor the headings in `text` would make resolvable."""
    return {anchor_slug(m.group(2)) for m in _HEADING_RE.finditer(text)}


def dangling_anchors(text: str) -> set[str]:
    """Anchors referenced by `text` that no heading in `text` provides."""
    referenced = {m.group(1).lower() for m in _ANCHOR_REF_RE.finditer(text)}
    return referenced - live_anchors(text)


def rewrite_dangling_anchors(text: str, changelog_url: str) -> tuple[str, set[str]]:
    """Point every dangling in-page anchor at the full CHANGELOG instead.

    Returns the rewritten text and the set of anchors that were rewritten, so the caller can
    report the substitution rather than performing it silently.
    """
    dangling = dangling_anchors(text)
    if not dangling:
        return text, set()

    def sub(m: re.Match[str]) -> str:
        anchor = m.group(1)
        if anchor.lower() not in dangling:
            return m.group(0)
        return f']({changelog_url}#{anchor})'

    return _ANCHOR_REF_RE.sub(sub, text), dangling


def build_body(section: str, changelog_url: str, cap: int = DEFAULT_CAP) -> tuple[str, list[str]]:
    """Return `(body, notes)` — the release body, and what was done to it.

    `notes` is never empty when the body differs from `section`: every reduction is reported
    so the stage can record it. A silently shortened body is the failure mode this module
    exists to prevent, so "changed nothing" and "changed something" must be distinguishable
    by the caller without diffing.
    """
    notes: list[str] = []

    if len(section) <= cap:
        body, rewritten = rewrite_dangling_anchors(section, changelog_url)
        if rewritten:
            notes.append(f'rewrote dangling anchors to the CHANGELOG: {sorted(rewritten)}')
        return body, notes

    sections = split_sections(section)
    by_title = {s.title: s for s in sections}
    kept = [by_title[t].text for t in KEPT_HEADINGS if t in by_title]

    if kept:
        notes.append(f'section is {len(section)} B > {cap} B — kept {KEPT_HEADINGS}')
        missing = [t for t in KEPT_HEADINGS if t not in by_title]
        if missing:
            notes.append(f'no such section to keep: {missing}')
        body = '\n'.join(kept)
    else:
        # Nothing recognisable to lift. Cut at a heading boundary rather than mid-sentence,
        # so the body still ends on a complete thought.
        notes.append(f'section is {len(section)} B > {cap} B and has none of {KEPT_HEADINGS}')
        acc: list[str] = []
        used = 0
        for s in sections:
            if used + len(s.text) > cap:
                break
            acc.append(s.text)
            used += len(s.text)
        body = '\n'.join(acc) if acc else section[:cap]
        if not acc:
            notes.append('no section boundary fit the cap — truncated at the byte limit')

    footer = (
        '\n\n---\n\n'
        f'📄 **This release has a large changelog.** The full entry — every Added / Fixed /\n'
        f'Changed item — is in [CHANGELOG.md]({changelog_url}).\n'
    )
    body = body.rstrip() + footer

    body, rewritten = rewrite_dangling_anchors(body, changelog_url)
    if rewritten:
        notes.append(f'rewrote dangling anchors to the CHANGELOG: {sorted(rewritten)}')

    # Guarantee the contract even if Overview + Upgrade Notes are themselves oversized.
    if len(body) > cap:
        body = body[:cap]
        notes.append(f'kept sections still exceeded {cap} B — hard-truncated')

    return body, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--changelog-url', required=True)
    ap.add_argument('--cap', type=int, default=DEFAULT_CAP)
    ap.add_argument('--section-file', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    if args.cap > GITHUB_BODY_CAP:
        print(
            f"refusing --cap {args.cap}: above GitHub's own limit ({GITHUB_BODY_CAP}); "
            'a cap that cannot be enforced is not a cap',
            file=sys.stderr,
        )
        return 2

    with open(args.section_file, encoding='utf-8') as fh:
        section = fh.read()
    if not section.strip():
        print('empty CHANGELOG section — nothing to publish', file=sys.stderr)
        return 2

    body, notes = build_body(section, args.changelog_url, args.cap)
    with open(args.out, 'w', encoding='utf-8') as fh:
        fh.write(body)

    # stdout is reserved for nothing; every message goes to stderr so a caller that captures
    # this command's output never picks up a banner as data (the generate_sbom lesson).
    for n in notes:
        print(f'[release-notes] {n}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
