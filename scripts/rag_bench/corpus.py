"""Resolve the benchmark user, load an injected corpus and fingerprint its injection identity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rag_bench.baselines import resolve_baseline_metrics_path
from rag_bench.config import logger


def resolve_user_id(email: str) -> int:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.db.base import engine
    from app.models.user import User

    with Session(engine) as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            raise SystemExit(f'No user with email {email!r} on the target stack.')
        return int(user.id)


def load_corpus(key: str, manifest_root: Path, data_dir: Path):
    """Manifest + queries + turns for one corpus key."""
    from tests.eval.harness import corpora as corpora_mod

    manifest_dir = manifest_root / key
    if not (manifest_dir / 'manifest.json').is_file():
        raise SystemExit(
            f'No injection manifest at {manifest_dir}. Inject the corpus first:\n'
            f'  ./scripts/inject-eval-corpus.sh --fresh <name> --corpus {key}'
        )
    corpus = corpora_mod.load_manifest(manifest_dir)
    turns = corpora_mod.load_turns(manifest_dir)
    if key == 'qmsum':
        queries = corpora_mod.load_qmsum_queries(corpus)
    elif key == 'synthetic':
        # The manifest records the exact directory that was injected, which is a
        # *rung* under $RAG_EVAL_DATA_DIR/synthetic (otsynth-core-v1/, ...), not
        # that directory itself. Prefer it, and fall back to the data dir only so
        # a manifest written on another machine still resolves.
        source = (
            corpus.root if (corpus.root / 'queries.jsonl').is_file() else data_dir / 'synthetic'
        )
        if not (source / 'queries.jsonl').is_file():
            raise SystemExit(
                f'No queries.jsonl under {corpus.root} or {data_dir / "synthetic"}. '
                f'The synthetic corpus must be readable to resolve its gold sets.'
            )
        queries = corpora_mod.load_synthetic_queries(corpus, source)
    elif key == 'miracl':
        # The language is recorded per meeting at injection time; every passage in
        # one manifest shares it. Read it from the manifest rather than taking a
        # flag, so scoring cannot silently be pointed at a different language from
        # the one that was injected.
        languages = {
            str(extra.get('miracl_language') or '') for extra in corpus.extra_by_meeting.values()
        } - {''}
        if len(languages) != 1:
            raise SystemExit(
                f'Expected exactly one MIRACL language in {manifest_dir}, found '
                f'{sorted(languages) or "none"}. Inject one language per manifest — '
                'mixing them would average nDCG across languages that are not comparable.'
            )
        queries = corpora_mod.load_miracl_queries(corpus, languages.pop())
    else:
        raise SystemExit(f'No query loader for corpus {key!r} (it ships no relevance judgements).')
    return corpus, turns, queries


def fingerprint(entries: list[dict[str, Any]]) -> str:
    """A stable, order-independent digest of an injection-identity entry list."""
    import hashlib

    canonical = json.dumps(entries, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]


def scan_injection_identity(
    client: Any, index: str, manifest_root: Path, scored_keys: set[str]
) -> dict[str, Any]:
    """Every corpus manifest actually present in the measured index, scored or not.

    ``--corpus`` only lists what gets SCORED — a haystack-only corpus like ``ami`` ships
    no query loader (see the ``else`` branch in :func:`load_corpus`) and can never appear
    there — but a distractor corpus injected alongside a scored one still changes what
    every retrieval number MEASURES: the haystack a query has to be found IN. Two runs
    with the same scored corpus and a different distractor composition are not
    comparable, and nothing about ``metrics.json`` would show that on its own; this is
    what ``--compare-only`` refuses on rather than warns about (see ``run_compare_only``
    and ``rag-evaluation.md``'s "AMI distractor haystack" section).

    Recorded in ``runinfo.json``, never ``metrics.json`` — like every other
    run-circumstance field, it is not part of the deterministic scoring claim.

    A manifest directory on disk with ZERO of its files present in the measured index is
    excluded rather than recorded as ``files_present_in_index: 0`` — it may be a manifest
    left over from an unrelated injection (a different ``--fresh`` stack, a deleted
    corpus), and an empty entry would be indistinguishable from "present but retrieved
    nothing" in a later read of the file. This is also the mechanism that keeps stale
    sibling manifests out of a fresh, empty-at-first ``--fresh`` stack's identity: only
    what actually landed in THIS index counts.
    """
    from tests.eval.harness import corpora as corpora_mod
    from tests.eval.harness import index_reader as index_reader_mod

    entries: list[dict[str, Any]] = []
    if manifest_root.is_dir():
        for child in sorted(manifest_root.iterdir()):
            if not (child / 'manifest.json').is_file():
                continue
            try:
                corpus = corpora_mod.load_manifest(child)
            except (OSError, ValueError, KeyError) as exc:
                logger.warning('injection_identity: could not read %s: %s', child, exc)
                continue
            chunks = index_reader_mod.fetch_chunks(client, index, corpus.file_uuids)
            present = sum(1 for v in chunks.values() if v)
            if present == 0:
                continue
            entries.append(
                {
                    'key': corpus.key,
                    'version': corpus.version,
                    'meetings_in_manifest': len(corpus.file_uuid_by_meeting),
                    'files_present_in_index': present,
                    'scored': corpus.key in scored_keys,
                }
            )
    entries.sort(key=lambda e: str(e['key']))
    return {'corpora': entries, 'fingerprint': fingerprint(entries)}


def refuse_on_differing_injection_identity(name_a: str, name_b: str) -> str | None:
    """``None`` if safe to compare; otherwise the refusal message for ``--compare-only``.

    Reads each baseline's ``runinfo.json`` (same directory as its ``metrics.json`` — see
    ``resolve_baseline_metrics_path``). Refuses outright when BOTH sides recorded an
    identity and they differ — a distractor-extended run compared against a QMSum-only
    run would report a delta that is actually a haystack-composition change, and #461's
    own instruction is that this must refuse, not warn. When either side predates this
    field (no ``runinfo.json``, or one without ``injection_identity`` — every baseline
    committed before this landed), comparison proceeds with a loud warning instead: there
    is no fingerprint to disagree with, and refusing every legacy baseline would be a
    regression, not a safety improvement.
    """

    def _load_identity(name: str) -> dict[str, Any] | None:
        runinfo_path = resolve_baseline_metrics_path(name).with_name('runinfo.json')
        if not runinfo_path.is_file():
            return None
        try:
            runinfo = json.loads(runinfo_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        identity = runinfo.get('injection_identity') if isinstance(runinfo, dict) else None
        return identity if isinstance(identity, dict) else None

    identity_a = _load_identity(name_a)
    identity_b = _load_identity(name_b)
    if identity_a is None or identity_b is None:
        logger.warning(
            '--compare-only: injection identity unavailable for %r and/or %r (predates '
            '#461 A5) — comparison proceeding WITHOUT verifying the two runs measured '
            'the same corpus composition.',
            name_a,
            name_b,
        )
        return None
    if identity_a.get('fingerprint') != identity_b.get('fingerprint'):
        return (
            f'--compare-only: {name_a!r} and {name_b!r} were measured against DIFFERENT '
            f'injected corpus compositions — refusing rather than reporting a delta that '
            f'is actually a haystack change.\n'
            f'  {name_a}: {identity_a.get("corpora")}\n'
            f'  {name_b}: {identity_b.get("corpora")}'
        )
    return None
