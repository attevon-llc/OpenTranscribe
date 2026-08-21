"""Loading eval queries and their gold spans, for each corpus we can score.

A corpus contributes queries only if it ships **relevance judgements**. The
injection manifest (``.rag-403/injections/<corpus>/files.jsonl``) is what ties a
source meeting id to the ``file_uuid`` the app actually indexed, so the same
loader works for any corpus the injector can ingest.

Licence tier travels with every query. It is what lets Stage 8 split publishable
from internal-only tables mechanically instead of from memory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from tests.eval.harness.answers import Answer
from tests.eval.harness.qrels import GoldSpan

#: The four #403 query classes. Stored underscored so a class name is a safe
#: key in JSON, a filename and a pandas-free table header alike.
LOOKUP = "lookup"
MULTI_FILE = "multi_file"
SUMMARIZE = "summarize"
AGGREGATION = "aggregation"
CLASSES = (LOOKUP, MULTI_FILE, SUMMARIZE, AGGREGATION)

#: The four #461 W2.E1 classes. Deliberately kept OUT of :data:`CLASSES` —
#: ``report.build_rows``/``build_answer_rows`` iterate that tuple to build the
#: retrieval and EM tables, and none of these four score on either engine (see
#: ``tests.eval.harness.attribution`` / ``tests.eval.harness.recurrence``). Adding
#: them there would silently create empty or wrongly-scored rows in tables that
#: already have a documented meaning.
#:
#: SPEAKER_ATTR / SPEAKER_SUMMARY are carved from QMSum's own queries (see
#: :func:`load_qmsum_queries`); ATTRIBUTION_PROBE is planted per SPEAKER_ATTR case.
#: RECURRENCE is scored at the corpus/scope level, not per query — it never
#: produces an :class:`EvalQuery` at all; see ``harness.recurrence`` directly.
SPEAKER_ATTR = "speaker_attr"
SPEAKER_SUMMARY = "speaker_summary"
ATTRIBUTION_PROBE = "attribution_probe"
RECURRENCE = "recurrence"
WAVE2_CLASSES = (SPEAKER_ATTR, SPEAKER_SUMMARY, ATTRIBUTION_PROBE, RECURRENCE)

#: QMSum specific queries whose text opens with one of these are summary
#: requests over a discussion span; everything else asks for a fact. A surface
#: rule, applied identically to all 1,576, and recorded in the results file.
_SUMMARY_PREFIXES = ("summarize", "summarise", "describe")

#: A LOOKUP or SUMMARIZE query matching this is a candidate for SPEAKER_ATTR /
#: SPEAKER_SUMMARY (#461 W2.E1) — but reclassification also requires its gold
#: span to resolve to exactly one speaker (:func:`_resolve_single_speaker`).
#: Measured against 3 QMSum ``Committee`` meetings while this was written: 24 of
#: 516 (4.65%) specific queries matched.
_ATTRIBUTION_PATTERN = re.compile(
    r"according to\s+\S"
    r"|what (?:did|does)\s+.{1,60}?\s+say\b"
    r"|\bwho said\b"
    r"|\bsaid by\b"
    r"|\bsuggested by\b"
    r"|'s\s+(?:opinion|view|views|suggestion|comment|proposal)\b",
    re.IGNORECASE,
)


def _resolve_single_speaker(meeting_transcripts: list, relevant_text_span) -> str | None:
    """The one speaker who spoke every turn in ``relevant_text_span``, or ``None``.

    ``None`` covers three cases deliberately collapsed together: the span is
    spoken by more than one speaker (not a clean attribution — e.g. a back-and-forth
    the gold turns straddle), an index falls outside ``meeting_transcripts`` (a
    malformed span), or the span is empty. All three mean "cannot derive a single
    gold speaker", and a caller must not attribute a query it cannot cleanly resolve
    — reclassifying it anyway would plant a wrong answer as ground truth.
    """
    speakers: set[str] = set()
    for pair in relevant_text_span or []:
        if len(pair) != 2:
            return None
        try:
            start, end = int(str(pair[0])), int(str(pair[1]))
        except ValueError:
            return None
        if end < start:
            return None
        for idx in range(start, end + 1):
            if idx < 0 or idx >= len(meeting_transcripts):
                return None
            speakers.add(str(meeting_transcripts[idx].get("speaker") or ""))
    if len(speakers) != 1:
        return None
    (only,) = speakers
    return only or None


def _pick_decoy_speaker(meeting_transcripts: list, true_speaker: str) -> str | None:
    """The lexicographically first OTHER speaker in the meeting — deterministic,
    no randomness, so a probe corpus reproduces byte-for-byte across runs."""
    candidates = sorted(
        {str(turn.get("speaker") or "") for turn in meeting_transcripts} - {true_speaker}
    )
    candidates = [name for name in candidates if name]
    return candidates[0] if candidates else None


def _carve_attribution(
    base_class: str, text: str, meeting_transcripts: list, relevant_text_span
) -> tuple[str, str | None]:
    """Reclassify a LOOKUP/SUMMARIZE QMSum query as SPEAKER_ATTR/SPEAKER_SUMMARY.

    Returns ``(base_class, None)`` — i.e. no change — unless BOTH the query text
    matches :data:`_ATTRIBUTION_PATTERN` AND the gold span resolves to exactly one
    speaker. Either condition failing means the query keeps scoring exactly as it
    does today; this function only ever narrows, never guesses.
    """
    if base_class not in (LOOKUP, SUMMARIZE):
        return base_class, None
    if not _ATTRIBUTION_PATTERN.search(text):
        return base_class, None
    speaker = _resolve_single_speaker(meeting_transcripts, relevant_text_span)
    if speaker is None:
        return base_class, None
    reclassified = SPEAKER_ATTR if base_class == LOOKUP else SPEAKER_SUMMARY
    return reclassified, speaker


@dataclass(frozen=True)
class EvalQuery:
    """One scoreable query.

    ``scored_on`` decides which engine scores it, and mixing engines in one table
    is exactly what each new ``scored_on`` value here exists to prevent:
    ``retrieval`` goes to :mod:`tests.eval.harness.metrics` against gold
    ``spans``; ``answer`` goes to :mod:`tests.eval.harness.answers` against
    ``gold_answer``; ``attribution``/``speaker_summary``/``attribution_probe`` go
    to :mod:`tests.eval.harness.attribution` (#461 W2.E1); ``answer_text`` goes to
    :mod:`tests.eval.harness.answer_text` and, opt-in, the judged tier — the label
    judge in :mod:`tests.eval.harness.answer_judge` against ``gold_text``, plus
    :mod:`tests.eval.harness.faithfulness_judge` (#463/#518). An aggregation query's ranked
    numbers are context, not its result (``.rag-403/synthetic-tier-design.md`` §12).
    """

    query_id: str
    text: str
    query_class: str
    corpus: str
    license_tier: str
    spans: tuple[GoldSpan, ...]
    scored_on: str = "retrieval"
    #: Generator rule that built it (``R3-agg-count-files``, ...). Empty for
    #: corpora that do not publish one; used to break the answer table down by
    #: rule, because "aggregation" averages five different questions.
    rule: str = ""
    #: The exact answer, for ``scored_on == "answer"`` (and the #461 W2.E1
    #: attribution-family) queries only.
    gold_answer: Answer | None = None
    #: The gold FREE-TEXT answer, for ``scored_on == "answer_text"`` queries only
    #: (#463) — QMSum's own human-written answer, scored by
    #: :mod:`tests.eval.harness.answer_text` (ROUGE/token-F1, deterministic) and,
    #: opt-in, :mod:`tests.eval.harness.answer_judge` (the FULL/PARTIAL/NONE/REFUSED
    #: label judge — superseded RAGAS ``answer_correctness``).
    gold_text: str | None = None


@dataclass
class InjectedCorpus:
    """What an injection manifest says is on the stack."""

    key: str
    name: str
    version: str
    license_tier: str
    root: Path
    file_uuid_by_meeting: dict[str, str]
    extra_by_meeting: dict[str, dict]

    @property
    def file_uuids(self) -> list[str]:
        return sorted(self.file_uuid_by_meeting.values())


def load_manifest(manifest_dir: Path) -> InjectedCorpus:
    """Read an injection manifest directory written by ``corpus_injection``."""
    manifest = json.loads((manifest_dir / "manifest.json").read_text(encoding="utf-8"))
    corpus = manifest["corpus"]
    by_meeting: dict[str, str] = {}
    extra: dict[str, dict] = {}
    for line in (manifest_dir / "files.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        by_meeting[str(record["meeting_id"])] = str(record["file_uuid"])
        extra[str(record["meeting_id"])] = record.get("extra") or {}
    return InjectedCorpus(
        key=str(corpus["key"]),
        name=str(corpus["name"]),
        version=str(corpus["version"]),
        license_tier=str(corpus.get("license_tier") or "unknown"),
        root=Path(str(corpus["root"])),
        file_uuid_by_meeting=by_meeting,
        extra_by_meeting=extra,
    )


def load_turns(manifest_dir: Path):
    """``file_uuid -> [TurnRow]`` from the manifest's ``turns.jsonl``."""
    from tests.eval.harness.qrels import TurnRow

    by_file: dict[str, list[TurnRow]] = {}
    for line in (manifest_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        by_file.setdefault(str(row["file_uuid"]), []).append(
            TurnRow(
                file_uuid=str(row["file_uuid"]),
                turn_index=int(row["turn_index"]),
                speaker=str(row["speaker"]),
                start=float(row["start"]),
                end=float(row["end"]),
                word_count=int(row["word_count"]),
            )
        )
    return by_file


def _qmsum_class(text: str) -> str:
    lowered = text.strip().lower()
    return SUMMARIZE if lowered.startswith(_SUMMARY_PREFIXES) else LOOKUP


def load_qmsum_queries(corpus: InjectedCorpus) -> list[EvalQuery]:
    """QMSum's 1,576 human ``specific_query_list`` entries, for injected files.

    ``general_query_list`` ("Summarize the whole meeting") is **excluded**: those
    234 queries ship no ``relevant_text_span``, so there is nothing to score them
    against. Counting them would mean inventing a gold set — exactly the thing a
    qrels file must never do.

    ``relevant_text_span`` values are ``[[start, end]]`` turn indices as decimal
    strings with an inclusive end; both are preserved verbatim into
    :class:`GoldSpan`, whose ``turn_indices`` does the ``+1``.

    **Also carves SPEAKER_ATTR / SPEAKER_SUMMARY and plants ATTRIBUTION_PROBE**
    (#461 W2.E1) from the SAME queries and the SAME already-loaded meeting JSON —
    no new data, no network access, license-free relative to the QMSum data already
    required to run this loader at all. A query is reclassified only when
    :func:`_carve_attribution` resolves a single gold speaker; every SPEAKER_ATTR
    case that resolves a decoy (:func:`_pick_decoy_speaker`) also emits one
    companion ATTRIBUTION_PROBE query. Retrieval ``spans`` are kept on every
    reclassified query too, even though no builder currently scores them on
    retrieval — provenance a later reader might want, at zero cost to keep.
    """
    queries: list[EvalQuery] = []
    for meeting_id in sorted(corpus.file_uuid_by_meeting):
        file_uuid = corpus.file_uuid_by_meeting[meeting_id]
        domain = str(corpus.extra_by_meeting.get(meeting_id, {}).get("domain") or "")
        path = corpus.root / "data" / domain / "all" / f"{meeting_id}.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        meeting_transcripts = payload.get("meeting_transcripts") or []
        for position, entry in enumerate(payload.get("specific_query_list") or []):
            spans = tuple(
                GoldSpan(file_uuid, int(str(pair[0])), int(str(pair[1])))
                for pair in entry.get("relevant_text_span") or []
                if len(pair) == 2
            )
            if not spans:
                continue
            text = str(entry.get("query") or "").strip()
            base_class = _qmsum_class(text)
            query_class, gold_speaker = _carve_attribution(
                base_class, text, meeting_transcripts, entry.get("relevant_text_span")
            )
            scored_on = "retrieval"
            gold_answer: Answer | None = None
            if query_class in (SPEAKER_ATTR, SPEAKER_SUMMARY):
                # `_carve_attribution` only returns a non-`retrieval`/`summarize`
                # `query_class` alongside a resolved speaker — the two are set
                # together — so this narrows `str | None` to `str` for mypy without
                # weakening the runtime contract with a redundant `if speaker:`.
                assert gold_speaker is not None, (
                    f"_carve_attribution returned {query_class!r} with no speaker"
                )
                scored_on = "attribution" if query_class == SPEAKER_ATTR else "speaker_summary"
                gold_answer = Answer.speaker(gold_speaker)
            queries.append(
                EvalQuery(
                    query_id=f"qmsum:{meeting_id}:{position:03d}",
                    text=text,
                    query_class=query_class,
                    corpus=corpus.key,
                    license_tier=corpus.license_tier,
                    spans=spans,
                    scored_on=scored_on,
                    gold_answer=gold_answer,
                )
            )
            if query_class == SPEAKER_ATTR:
                assert gold_speaker is not None
                decoy = _pick_decoy_speaker(meeting_transcripts, gold_speaker)
                if decoy is not None:
                    queries.append(
                        EvalQuery(
                            query_id=f"qmsum-probe:{meeting_id}:{position:03d}",
                            text=f'Was this said by {decoy}? — "{text}"',
                            query_class=ATTRIBUTION_PROBE,
                            corpus=corpus.key,
                            license_tier=corpus.license_tier,
                            spans=spans,
                            scored_on="attribution_probe",
                            gold_answer=Answer.attribution_probe(gold_speaker, decoy),
                        )
                    )
    return queries


def load_qmsum_answer_queries(corpus: InjectedCorpus) -> list[EvalQuery]:
    """QMSum's human-written answers, for #463's answer-quality tier.

    Two sources, both from the SAME already-loaded meeting JSON
    :func:`load_qmsum_queries` reads — no new data:

    * ``specific_query_list[].answer`` — one ``scored_on="answer_text"`` query per
      specific query, ``gold_text`` = the human answer. ``spans`` are kept, THE
      SAME turn ranges :func:`load_qmsum_queries` scores retrieval against — here
      they are faithfulness CONTEXT (the turns a faithful answer should stay
      inside), not a retrieval judgement. This is a separate, additive query set:
      it does not replace or mutate :func:`load_qmsum_queries`'s output, so a
      caller can score retrieval AND answer-text/judged measures for the same
      underlying question without a ``scored_on`` collision — the same pattern
      the SPEAKER_ATTR carving already established.
    * ``general_query_list`` ("Summarize the whole meeting") — EXCLUDED from
      :func:`load_qmsum_queries` (no ``relevant_text_span`` to score retrieval
      against) but INCLUDED here: ``SUMMARIZE``-class, ``gold_text`` = the human
      whole-meeting summary, ``spans`` a single ``GoldSpan(file_uuid, 0, 0)``
      naming the file (MIRACL's whole-document convention) so a
      ``--scope gold-files`` consumer (``runner.py`` line ~365) restricts
      retrieval to exactly this one file. "Summarize the whole meeting" asked
      against the full corpus is a different, undefined task — corpus-wide scope
      would silently change what the query means.

    Verified while this was written, against the real QMSum data
    (696 meeting files, every domain/split): **0 missing ``answer`` fields across
    4,728 specific queries** — an empty/missing answer is skipped defensively
    below anyway, but none exist. **``data/ALL/`` has no ``all/`` split
    subdirectory at all** (only ``jsonl``/``test``/``train``/``val``) — so the
    ``data/{domain}/all/{meeting_id}.json`` path :func:`load_qmsum_queries`
    already uses is the only one that ever resolves, and this loader follows the
    same convention rather than trying ``ALL/all`` and finding nothing.
    """
    queries: list[EvalQuery] = []
    for meeting_id in sorted(corpus.file_uuid_by_meeting):
        file_uuid = corpus.file_uuid_by_meeting[meeting_id]
        domain = str(corpus.extra_by_meeting.get(meeting_id, {}).get("domain") or "")
        path = corpus.root / "data" / domain / "all" / f"{meeting_id}.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))

        for position, entry in enumerate(payload.get("specific_query_list") or []):
            answer_text = str(entry.get("answer") or "").strip()
            if not answer_text:
                continue
            spans = tuple(
                GoldSpan(file_uuid, int(str(pair[0])), int(str(pair[1])))
                for pair in entry.get("relevant_text_span") or []
                if len(pair) == 2
            )
            text = str(entry.get("query") or "").strip()
            queries.append(
                EvalQuery(
                    query_id=f"qmsum-answer:{meeting_id}:{position:03d}",
                    text=text,
                    query_class=_qmsum_class(text),
                    corpus=corpus.key,
                    license_tier=corpus.license_tier,
                    spans=spans,
                    scored_on="answer_text",
                    gold_text=answer_text,
                )
            )

        for position, entry in enumerate(payload.get("general_query_list") or []):
            answer_text = str(entry.get("answer") or "").strip()
            if not answer_text:
                continue
            queries.append(
                EvalQuery(
                    query_id=f"qmsum-general:{meeting_id}:{position:03d}",
                    text=str(entry.get("query") or "Summarize the whole meeting.").strip(),
                    query_class=SUMMARIZE,
                    corpus=corpus.key,
                    license_tier=corpus.license_tier,
                    spans=(GoldSpan(file_uuid, 0, 0),),
                    scored_on="answer_text",
                    gold_text=answer_text,
                )
            )
    return queries


def load_synthetic_queries(corpus: InjectedCorpus, source_dir: Path) -> list[EvalQuery]:
    """Synthetic-tier queries, remapped onto the uuids the app assigned.

    The generator publishes its own ``file_uuid`` per meeting; injection derives
    a different one (``uuid5`` over corpus+seed+meeting id). ``meeting_key`` is
    the join, so the corpus's shards are read once to build the alias table.

    Gold turn ranges use QMSum's inclusive convention on purpose, so this shares
    :class:`GoldSpan` with the QMSum loader and no second overlap rule exists.
    """
    alias: dict[str, str] = {}
    for shard in sorted((source_dir / "meetings").glob("*.jsonl")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            app_uuid = corpus.file_uuid_by_meeting.get(str(record["meeting_key"]))
            if app_uuid:
                alias[str(record["file_uuid"])] = app_uuid

    queries: list[EvalQuery] = []
    for line in (source_dir / "queries.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        built = (
            _answer_query(record, corpus, alias)
            if str(record.get("scored_on") or "retrieval") == "answer"
            else _retrieval_query(record, corpus, alias)
        )
        if built is not None:
            queries.append(built)
    return queries


def _retrieval_query(record: dict, corpus: InjectedCorpus, alias: dict[str, str]):
    """A ranked-retrieval query, or ``None`` if its gold set is not fully indexed."""
    spans: list[GoldSpan] = []
    for corpus_uuid, ranges in (record.get("gold_turns") or {}).items():
        app_uuid = alias.get(str(corpus_uuid))
        # A query whose gold set is only partly on the stack cannot be scored:
        # its recall denominator would silently shrink to whatever was injected.
        if not app_uuid:
            return None
        spans.extend(GoldSpan(app_uuid, int(pair[0]), int(pair[1])) for pair in ranges)
    if not spans:
        return None
    return EvalQuery(
        query_id=f"synthetic:{record['query_id']}",
        text=str(record["text"]),
        query_class=str(record["query_class"]),
        corpus=corpus.key,
        license_tier=corpus.license_tier,
        spans=tuple(spans),
        rule=str(record.get("rule") or ""),
    )


def _answer_query(record: dict, corpus: InjectedCorpus, alias: dict[str, str]):
    """An answer-scored query, or ``None`` if the stack cannot support its answer.

    The bar is higher than for a retrieval query, deliberately: the answer is a
    property of **the whole corpus**, so a missing file does not merely shrink a
    recall denominator, it changes the correct answer. Two rules:

    * every ``gold_files`` entry must be indexed — otherwise the true count is
      not the published count;
    * every ``related_files`` entry must be indexed too. Those are R7's
      out-of-month mentions of the same marker. Without them "how many meetings
      **in March**" has the same answer as "how many meetings", and the query
      stops testing the filter that is its entire point.
    """
    wanted = [str(u) for u in record.get("gold_files") or []]
    wanted += [str(u) for u in record.get("related_files") or []]
    if not wanted or any(uuid not in alias for uuid in wanted):
        return None
    try:
        gold = Answer.from_record(
            str(record.get("answer_kind") or ""), record.get("answer"), remap=alias
        )
    except (ValueError, KeyError, TypeError):
        # An answer shape the scorer does not know is a gap in the harness, not a
        # zero for the system: it is dropped, never scored 0.
        return None
    return EvalQuery(
        query_id=f"synthetic:{record['query_id']}",
        text=str(record["text"]),
        query_class=str(record["query_class"]),
        corpus=corpus.key,
        license_tier=corpus.license_tier,
        spans=(),
        scored_on="answer",
        rule=str(record.get("rule") or ""),
        gold_answer=gold,
    )


def load_miracl_queries(
    corpus: InjectedCorpus, language: str, split: str = "dev"
) -> list[EvalQuery]:
    """MIRACL queries for one language, remapped onto the uuids the app indexed (#453).

    **The gold shape is different from every other corpus here, and the injection is
    what reconciles it.** QMSum and the synthetic tier judge an inclusive *turn range*
    inside a meeting; MIRACL judges a whole *passage* — "docid 8156619#0 is relevant to
    query 10036600#0". Rather than teach :class:`GoldSpan` a second convention, the
    injector writes **one passage per file, as a single turn**, so a document-level
    judgement is exactly ``GoldSpan(uuid, 0, 0)``. ``qrels.py`` and ``metrics.py`` need
    no changes at all, and there is still only one overlap rule in the harness.

    ``meeting_key`` in the injection manifest is the MIRACL ``docid``.

    ⚠️ **Negatives are scored, not filtered.** MIRACL's explicit ``relevance: 0``
    judgements are the reason it is the anchor corpus — a run over only the positives
    makes every retriever look perfect because there is nothing else to rank. They are
    carried through into the qrels the metric engine consumes.

    ⚠️ **Every query is ``LOOKUP``.** MIRACL asks a factual question against a passage
    collection; none of its queries are summarisation or aggregation. Labelling them
    otherwise would put them in tables whose other rows mean something different.
    """
    from tests.eval.harness import miracl

    topics = miracl.load_topics(corpus.root, language, split)
    qrels = miracl.load_qrels(corpus.root, language, split)

    queries: list[EvalQuery] = []
    for qid in sorted(topics):
        judged = qrels.get(qid, {})
        spans = tuple(
            GoldSpan(file_uuid=corpus.file_uuid_by_meeting[docid], start_turn=0, end_turn=0)
            for docid, relevance in sorted(judged.items())
            # Only POSITIVES become gold spans; the negatives still sit in the index as
            # distractors, which is their whole job. A negative in the gold set would
            # invert the metric.
            if relevance > 0 and docid in corpus.file_uuid_by_meeting
        )
        if not spans:
            # A query whose positives were not injected cannot be scored, and including
            # it would depress every metric for a reason that is not retrieval quality.
            continue
        queries.append(
            EvalQuery(
                query_id=f"{language}:{qid}",
                text=topics[qid],
                query_class=LOOKUP,
                corpus=corpus.key,
                license_tier=corpus.license_tier,
                spans=spans,
            )
        )
    return queries
