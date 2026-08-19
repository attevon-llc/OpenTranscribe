"""Reading MIRACL — topics, qrels, and a bounded passage subset (#453).

MIRACL is the anchor for every non-English retrieval claim: 18 languages, Apache-2.0,
with **human pooled judgements and explicit negatives**. Nothing else in the corpus
directory has all three.

Three shapes, all verified on disk rather than assumed:

    topics.miracl-v1.0-<lang>-dev.tsv   qid <TAB> query text
    qrels.miracl-v1.0-<lang>-dev.tsv    qid <TAB> Q0 <TAB> docid <TAB> relevance
    miracl-corpus-v1.0-<lang>/*.jsonl.gz   {"docid": "7#0", "title": ..., "text": ...}

⚠️ **Score on ``dev``.** ``test-a``/``test-b`` ship topics but **no qrels**, so a run
against them produces a ranking nobody can grade — and it looks like a successful run.
:func:`load_topics` refuses a split whose qrels file is absent for exactly that reason.

⚠️ **Never expand a language corpus wholesale.** Spanish alone is **1.5 GB gzipped**,
and MIRACL ships ~106 M passages across the 18 languages. The corpus is streamed once
and only the judged docids are kept, which is what makes a per-language subset a
minutes-long operation instead of an afternoon and a full disk.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

#: Splits that ship relevance judgements. Anything else cannot be scored.
SCOREABLE_SPLITS = ("dev", "train")

#: The 18 languages MIRACL v1.0 publishes.
LANGUAGES = (
    "ar", "bn", "de", "en", "es", "fa", "fi", "fr", "hi",
    "id", "ja", "ko", "ru", "sw", "te", "th", "yo", "zh",
)  # fmt: skip


@dataclass(frozen=True)
class MiraclPassage:
    """One judged passage. ``docid`` is MIRACL's ``<page>#<paragraph>`` form."""

    docid: str
    title: str
    text: str


def language_dir(root: Path, language: str) -> Path:
    """Topics + qrels for one language."""
    return root / "miracl" / f"miracl-v1.0-{language}"


def corpus_dir(root: Path, language: str) -> Path:
    """The gzipped passage shards for one language."""
    return root / "miracl-corpus" / f"miracl-corpus-v1.0-{language}"


def load_topics(root: Path, language: str, split: str = "dev") -> dict[str, str]:
    """``qid -> query text`` for one language and split.

    Raises:
        ValueError: if the split ships no qrels. A run over ungraded topics returns a
            ranking that cannot be scored, and does so *successfully* — the exact shape
            of failure the eval harness exists to prevent.
        FileNotFoundError: if the topics file is absent.
    """
    if split not in SCOREABLE_SPLITS:
        raise ValueError(
            f"MIRACL split {split!r} ships no qrels, so a run over it cannot be scored "
            f"(and would report success anyway). Scoreable splits: {SCOREABLE_SPLITS}"
        )

    path = language_dir(root, language) / "topics" / f"topics.miracl-v1.0-{language}-{split}.tsv"
    topics: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            qid, _, text = line.partition("\t")
            if text:
                topics[qid] = text
    if not topics:
        raise ValueError(f"{path} parsed to zero topics — check the format has not changed")
    return topics


def load_qrels(root: Path, language: str, split: str = "dev") -> dict[str, dict[str, int]]:
    """``qid -> {docid: relevance}``, from the standard TREC 4-column form.

    Kept as a nested dict rather than flattened because relevance is *graded*: the
    grade is the gain in nDCG, and collapsing it to a set of relevant ids silently
    turns a graded metric into a binary one.
    """
    path = language_dir(root, language) / "qrels" / f"qrels.miracl-v1.0-{language}-{split}.tsv"
    qrels: dict[str, dict[str, int]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 4:
                continue
            qid, _, docid, relevance = parts
            qrels.setdefault(qid, {})[docid] = int(relevance)
    if not qrels:
        raise ValueError(f"{path} parsed to zero judgements — check the format has not changed")
    return qrels


def judged_docids(qrels: dict[str, dict[str, int]], qids: list[str]) -> set[str]:
    """Every docid judged for the given queries, relevant or not.

    **Negatives are kept deliberately.** MIRACL's explicit negatives are the reason it
    is the anchor corpus: a subset containing only the relevant passages makes every
    retriever look perfect, because there is nothing else to rank.
    """
    wanted: set[str] = set()
    for qid in qids:
        wanted.update(qrels.get(qid, {}))
    return wanted


def extract_passages(
    root: Path, language: str, docids: set[str], *, progress_every: int = 0
) -> dict[str, MiraclPassage]:
    """Stream the language's shards ONCE, keeping only ``docids``.

    Streaming rather than indexing is the point: the Spanish corpus alone is 1.5 GB
    gzipped, and the full MIRACL corpus is ~106 M passages. This reads each shard
    sequentially, holds only the wanted passages, and stops early once every requested
    docid has been found.

    Returns:
        ``docid -> MiraclPassage`` for every docid that was found. A caller comparing
        the returned size against ``len(docids)`` learns whether the corpus and the
        qrels actually agree — which is worth checking, because a silent shortfall
        would show up later as a retriever that simply cannot find the gold.
    """
    shards = sorted(corpus_dir(root, language).glob("*.jsonl.gz"))
    if not shards:
        raise FileNotFoundError(f"no passage shards under {corpus_dir(root, language)}")

    found: dict[str, MiraclPassage] = {}
    remaining = set(docids)
    scanned = 0
    for shard in shards:
        if not remaining:
            break
        with gzip.open(shard, "rt", encoding="utf-8") as handle:
            for line in handle:
                scanned += 1
                if progress_every and scanned % progress_every == 0:
                    print(f"  scanned {scanned:,} passages, {len(remaining):,} still wanted")
                # Cheap reject before paying for json.loads on ~106M lines.
                if not remaining:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                docid = row.get("docid")
                if docid in remaining:
                    remaining.discard(docid)
                    found[docid] = MiraclPassage(
                        docid=docid,
                        title=row.get("title", ""),
                        text=row.get("text", ""),
                    )
    return found


def build_subset(
    root: Path,
    language: str,
    *,
    query_count: int,
    split: str = "dev",
    cache_dir: Path | None = None,
    rebuild: bool = False,
) -> tuple[dict[str, str], dict[str, dict[str, int]], dict[str, MiraclPassage]]:
    """Topics, qrels and passages for the first ``query_count`` queries of a language.

    **The passage subset is CACHED, and that is not an optimisation detail.** Extraction
    streams the whole language corpus — measured at **78.5 s for Spanish** (1.5 GB
    gzipped), and the cost is the same whether 20 docids are wanted or 2000, because the
    scan runs until every one is found. Across 18 languages that is ~25 minutes per run.
    The baseline in #453 step 2 is re-run every time a retrieval knob moves, so paying
    that once and reading a small JSONL afterwards is the difference between a
    measurement anyone will actually repeat and one they will not.

    Queries are taken in **sorted qid order**, not sampled: the subset has to be
    identical across runs or two measurements are not comparable. If sampling is ever
    wanted it needs a recorded seed, for the same reason.

    Returns:
        ``(topics, qrels, passages)`` all restricted to the selected queries.
    """
    topics = load_topics(root, language, split)
    qrels = load_qrels(root, language, split)

    qids = sorted(topics)[:query_count]
    topics = {qid: topics[qid] for qid in qids}
    qrels = {qid: qrels[qid] for qid in qids if qid in qrels}
    wanted = judged_docids(qrels, qids)

    cache = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir / f"miracl-{language}-{split}-{query_count}.jsonl"

    if cache is not None and cache.exists() and not rebuild:
        passages = {}
        with cache.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                passages[row["docid"]] = MiraclPassage(**row)
        # A cache that does not cover the request is worse than no cache: it would
        # silently score against fewer documents than the qrels judge.
        if wanted.issubset(passages):
            return topics, qrels, {d: passages[d] for d in wanted}

    passages = extract_passages(root, language, wanted)

    missing = wanted - set(passages)
    if missing:
        # Not raised: MIRACL's qrels and corpus are published separately and a small
        # shortfall is possible. But it MUST be visible — a judged passage that is not
        # in the index cannot be retrieved, so it depresses recall for a reason that has
        # nothing to do with the retriever under test.
        print(
            f"  WARNING: {len(missing)} of {len(wanted)} judged passages are absent from "
            f"the {language} corpus; recall is capped below 1.0 for reasons unrelated to "
            "retrieval quality"
        )

    if cache is not None:
        with cache.open("w", encoding="utf-8") as handle:
            for passage in passages.values():
                handle.write(
                    json.dumps(
                        {"docid": passage.docid, "title": passage.title, "text": passage.text},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    return topics, qrels, passages
