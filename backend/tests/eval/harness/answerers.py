"""Where an aggregation answer comes from — never from a language model.

#403 is explicit: the aggregation class is answered from **OpenSearch
aggregations or Postgres**, never by an LLM counting, and **D6 makes the no-LLM
deployment first-class**. An answer-scoring path that needed a model would
contradict the property it exists to measure, so nothing here imports one.

Three answerers ship — a floor, a ceiling, and the thing being measured:

``NullAnswerer``
    Declines every query. This is the honest floor: as of Stage 1 the product has
    **no aggregation path at all**, so a stack measured through it scores 0.000
    EM on every count and list query, with ``answered`` 0.000 saying why. Stage 4
    has to move that number rather than inherit one.
``ReferenceAnswerer``
    A rules intent parser plus a terms aggregation and two SQL statements. It is
    the **instrument's control, not the product's answer** — it does not touch
    the chat path, and every results file it writes records that. Its value is
    that it establishes what this corpus's aggregation questions are worth when
    answered by a mechanism that is exact by construction: Stage 4's router has a
    number to beat and a per-rule breakdown of where the difficulty is.
``ProductAnswerer``
    **The product's own path** (#403 Stage 4) — ``services/chat/router.route``
    then ``services/chat/aggregation_service.answer_aggregation``, the same two
    calls a chat turn makes. It is the thing being measured, and it shares no
    intent parsing with the reference, which is the only reason comparing the two
    says anything at all.

All three are deterministic. Aggregations order by ``_key`` and refuse a
truncated bucket list rather than reporting a count that silently dropped a
shard's tail; every set is returned sorted.

**The reference's intent parser is matched to the generator's question frames**
and says so. That is a stated limit, not a hidden one: it recovers the *subject
phrase* from a natural-language question, and the phrase is never the answer —
the answer is a count or a file set that only the index or the database can
produce. The product recovers its subject by stripping a **generic**
interrogative frame instead, so it is expected to score below the reference; the
gap between them is a finding about the product, not noise in the instrument.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from tests.eval.harness.answers import Answer

logger = logging.getLogger(__name__)

#: Bucket ceiling for every aggregation. A truncated bucket list is a wrong
#: answer that looks like a right one, so :func:`_buckets` refuses rather than
#: silently reporting the head of the list.
MAX_BUCKETS = 10_000

#: Question frames -> intent. Ordered: the temporal frame is a prefix-distinct
#: superset of the plain count frame and is tried first regardless.
INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "temporal_count",
        re.compile(
            r"^how many meetings in (?P<month>[a-z]+) (?P<year>\d{4}) discussed (?P<phrase>.+?)\s*\?$",
            re.IGNORECASE,
        ),
    ),
    ("count_files", re.compile(r"^how many meetings discussed (?P<phrase>.+?)\s*\?$", re.I)),
    (
        "list_files",
        re.compile(r"^which meetings mention (?P<phrase>.+?)\s*\?\s*list them\.?$", re.I),
    ),
    (
        "count_events",
        re.compile(r"^how many times in total did we defer (?P<phrase>.+?)\s*\?$", re.I),
    ),
    (
        "speaker_top",
        re.compile(r"^who attended the most (?P<kind>.+?) sessions for (?P<team>.+?)\s*\?$", re.I),
    ),
)

_MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}

#: Mechanism per intent, recorded in the results file so a reader never has to
#: guess whether a number came out of the index or the database.
MECHANISM: dict[str, str] = {
    "count_files": "opensearch: match_phrase(content.exact) + terms(file_uuid) agg",
    "list_files": "opensearch: match_phrase(content.exact) + terms(file_uuid) agg",
    "temporal_count": "opensearch phrase agg INTERSECT postgres meeting-date filter",
    "count_events": "postgres: regexp_count over transcript_segment.text",
    "speaker_top": "opensearch: title phrase filter + terms(speakers) x terms(file_uuid)",
}


@dataclass(frozen=True)
class Intent:
    """A parsed question: what is being asked, and about what."""

    name: str
    slots: dict[str, str]


def parse_intent(text: str) -> Intent | None:
    """Recover the intent and its slots, or ``None`` if no rule matches.

    ``None`` means the answerer declines: the query is scored 0 and counted as
    unanswered. It is never guessed at.
    """
    stripped = " ".join(str(text).split())
    for name, pattern in INTENT_PATTERNS:
        found = pattern.match(stripped)
        if found:
            return Intent(name, {k: v.strip() for k, v in found.groupdict().items()})
    return None


def _buckets(container: dict[str, Any], path: str) -> list[dict[str, Any]]:
    """Bucket list for ``path``, refusing a truncated aggregation.

    Accepts either a whole response (aggregations under ``aggregations``) or one
    bucket (sub-aggregations sit directly on it) — the difference is a real
    OpenSearch response shape, and reading only the first form silently returned
    an empty bucket list for every sub-aggregation.
    """
    holder = container.get("aggregations")
    if not isinstance(holder, dict):
        holder = container
    agg = holder.get(path) or {}
    if int(agg.get("sum_other_doc_count") or 0) > 0:
        raise RuntimeError(
            f"Aggregation {path!r} truncated at {MAX_BUCKETS} buckets — the answer would be "
            "wrong in a way that looks right. Raise MAX_BUCKETS rather than reporting it."
        )
    return list(agg.get("buckets") or [])


def _posix_escape(value: str) -> str:
    """Escape a literal for a POSIX ERE, which is not Python's regex dialect."""
    return re.sub(r"([\\.^$*+?()\[\]{}|])", r"\\\1", value)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class NullAnswerer:
    """Answers nothing. The state of the product before Stage 4, measured."""

    name = "none"

    def describe(self) -> dict[str, Any]:
        return {
            "answerer": self.name,
            "summary": "declines every query — the pre-Stage-4 product floor",
            "is_production_path": False,
            "llm_required": False,
        }

    def answer(self, query) -> Answer | None:  # noqa: ARG002 - deliberately ignores it
        return None


class ReferenceAnswerer:
    """Rules intent + OpenSearch aggregations + Postgres. No model, no chat path.

    Args:
        client: An OpenSearch client (``search`` is the only method used).
        index: Chunk index name.
        user_id: Owner of the corpus; every query is filtered by the production
            ACL field so the measurement cannot see documents the user cannot.
        engine: SQLAlchemy engine for the two SQL-answered rules, or ``None`` —
            in which case those intents **decline** rather than answering from a
            weaker source.
    """

    name = "reference"

    def __init__(self, client: Any, index: str, user_id: int, engine: Any | None = None) -> None:
        self.client = client
        self.index = index
        self.user_id = int(user_id)
        self.engine = engine

    def describe(self) -> dict[str, Any]:
        return {
            "answerer": self.name,
            "summary": (
                "harness-side rules router + OpenSearch terms aggregations + Postgres. "
                "The instrument's control, NOT the product's chat path (which has no "
                "aggregation route until Stage 4)."
            ),
            "is_production_path": False,
            "llm_required": False,
            "acl_filter": "accessible_user_ids (the production ACL field)",
            "intents": sorted(name for name, _ in INTENT_PATTERNS),
            "mechanism": dict(sorted(MECHANISM.items())),
            "postgres_available": self.engine is not None,
            "hybrid_aggs": "never — OpenSearch 3.4 crashes on aggs over a hybrid body",
        }

    # ------------------------------------------------------------------ search

    def _phrase_files(self, phrase: str) -> list[str]:
        """Files whose chunk text contains ``phrase``, sorted. A plain agg."""
        body = {
            "size": 0,
            "track_total_hits": False,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"accessible_user_ids": self.user_id}},
                        {"match_phrase": {"content.exact": phrase}},
                    ]
                }
            },
            "aggs": {
                "files": {
                    "terms": {"field": "file_uuid", "size": MAX_BUCKETS, "order": {"_key": "asc"}}
                }
            },
        }
        response = self.client.search(index=self.index, body=body)
        return sorted(str(bucket["key"]) for bucket in _buckets(response, "files"))

    def _series_speaker_top(self, kind: str, team: str) -> tuple[str, int] | None:
        """Top attendee of a series, by distinct files, from a terms aggregation.

        Scope comes from the meeting **title** (``<Team> — <series kind> #n``),
        which is app metadata the indexer writes, not corpus-private knowledge.
        A tie is resolved lexicographically and will simply score 0 — the gold
        set guarantees a strict maximum, so a tie here is a wrong answer.
        """
        body = {
            "size": 0,
            "track_total_hits": False,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"accessible_user_ids": self.user_id}},
                        {"match_phrase": {"title": team}},
                        {"match_phrase": {"title": kind}},
                    ]
                }
            },
            "aggs": {
                "people": {
                    "terms": {"field": "speakers", "size": MAX_BUCKETS, "order": {"_key": "asc"}},
                    "aggs": {
                        "files": {
                            "terms": {
                                "field": "file_uuid",
                                "size": MAX_BUCKETS,
                                "order": {"_key": "asc"},
                            }
                        }
                    },
                }
            },
        }
        response = self.client.search(index=self.index, body=body)
        tally = [
            (str(bucket["key"]), len(_buckets(bucket, "files")))
            for bucket in _buckets(response, "people")
        ]
        if not tally:
            return None
        tally.sort(key=lambda item: (-item[1], item[0]))
        return tally[0]

    # ---------------------------------------------------------------- postgres

    def _occurrences(self, phrase: str) -> int | None:
        """Total occurrences of ``phrase`` across the user's transcript segments.

        Postgres, not OpenSearch: chunking overlaps a long turn's tail into the
        next chunk, so counting *occurrences* over chunk documents double-counts.
        Segments are the unsplit turns the chunker was built from.
        """
        if self.engine is None:
            return None
        from sqlalchemy import text as sql_text

        statement = sql_text(
            # regexp_count(string, pattern, start, flags): the third argument is
            # the 1-based start offset, NOT the flags — passing 'i' there is an
            # integer cast error, which is how this was found.
            "SELECT COALESCE(SUM(regexp_count(ts.text, :pattern, 1, 'i')), 0) "
            "FROM transcript_segment ts "
            "JOIN media_file mf ON mf.id = ts.media_file_id "
            "WHERE mf.user_id = :user_id AND ts.text ILIKE :like ESCAPE '\\'"
        )
        with self.engine.connect() as connection:
            found = connection.execute(
                statement,
                {
                    "pattern": _posix_escape(phrase),
                    "user_id": self.user_id,
                    "like": f"%{_like_escape(phrase)}%",
                },
            ).scalar()
        return int(found or 0)

    def _restrict_to_month(self, file_uuids: list[str], year: int, month: int) -> list[str] | None:
        """Those of ``file_uuids`` whose meeting date falls in the month.

        The date lives in ``media_file.metadata_important -> rag_eval -> date``,
        stamped by the injector. **This is the only place a meeting date exists on
        this stack** — no recorded-date column is populated for injected files —
        and a production answerer would read a real one. Recorded as a limitation
        rather than presented as the production mechanism.
        """
        if self.engine is None or not file_uuids:
            return None
        from sqlalchemy import text as sql_text

        statement = sql_text(
            "SELECT mf.uuid::text FROM media_file mf "
            "WHERE mf.user_id = :user_id AND mf.uuid::text = ANY(:uuids) "
            "AND (mf.metadata_important -> 'rag_eval' ->> 'date') LIKE :prefix"
        )
        with self.engine.connect() as connection:
            rows = connection.execute(
                statement,
                {
                    "user_id": self.user_id,
                    "uuids": list(file_uuids),
                    "prefix": f"{year:04d}-{month:02d}%",
                },
            ).scalars()
            return sorted(str(row) for row in rows)

    # ------------------------------------------------------------------ answer

    def answer(self, query) -> Answer | None:
        """Answer one :class:`~tests.eval.harness.corpora.EvalQuery`, or decline."""
        intent = parse_intent(query.text)
        if intent is None:
            logger.debug("No intent rule matched %r", query.text)
            return None

        if intent.name == "count_files":
            return Answer.integer(len(self._phrase_files(intent.slots["phrase"])))

        if intent.name == "list_files":
            return Answer.file_set(self._phrase_files(intent.slots["phrase"]))

        if intent.name == "count_events":
            total = self._occurrences(intent.slots["phrase"])
            return None if total is None else Answer.integer(total)

        if intent.name == "temporal_count":
            month = _MONTHS.get(intent.slots["month"].lower())
            if month is None:
                return None
            in_month = self._restrict_to_month(
                self._phrase_files(intent.slots["phrase"]), int(intent.slots["year"]), month
            )
            return None if in_month is None else Answer.integer(len(in_month))

        top = self._series_speaker_top(intent.slots["kind"], intent.slots["team"])
        return None if top is None else Answer.speaker_count(top[0], top[1])


class ProductAnswerer:
    """**The product's own aggregation path**, driven end to end (#403 Stage 4).

    ``NullAnswerer`` is the floor and ``ReferenceAnswerer`` is the ceiling; this
    is the thing being measured. It calls ``services/chat/router.route`` and then
    ``services/chat/aggregation_service.answer_aggregation`` — the same two
    functions a chat turn calls — and maps whatever comes back onto the gold
    answer shape.

    **It shares no code with `ReferenceAnswerer`, deliberately.** The reference's
    intent regexes are matched to the eval generator's exact question frames; the
    product recovers its subject by stripping a generic interrogative frame. If
    the product imported those regexes it would be scored against itself and the
    number would mean nothing. Expect it to score *below* the reference — the gap
    is the measurement.

    Args:
        client: OpenSearch client.
        index: Chunk index name.
        user_id: Owner of the corpus.
        engine: SQLAlchemy engine. ``None`` makes the Postgres-answered shapes
            decline rather than answer from a weaker source.
    """

    name = "product"

    def __init__(self, client: Any, index: str, user_id: int, engine: Any | None = None) -> None:
        self.client = client
        self.index = index
        self.user_id = int(user_id)
        self.engine = engine
        self.shapes: Counter = Counter()

    def describe(self) -> dict[str, Any]:
        from app.services.chat.aggregation import SHAPES

        return {
            "answerer": self.name,
            "summary": (
                "the PRODUCT's chat aggregation path: services/chat/router.route -> "
                "services/chat/aggregation_service.answer_aggregation. Shares no intent "
                "parsing with the reference answerer, which is what makes the comparison "
                "meaningful rather than circular."
            ),
            "is_production_path": True,
            "llm_required": False,
            "acl_filter": "accessible_user_ids + the Postgres accessible-files subquery",
            "shapes": list(SHAPES),
            "shapes_used": dict(sorted(self.shapes.items())),
            "hybrid_aggs": "never — OpenSearch 3.4 crashes on aggs over a hybrid body",
            "known_limitation": (
                "the temporal filter is a range on upload_time, the only date this "
                "application records. Injected corpus files carry their meeting date in "
                "metadata_important, which no product code reads, so date-filtered "
                "questions are expected to score 0 here until a recorded-date column exists."
            ),
        }

    def answer(self, query) -> Answer | None:
        """Route, aggregate, and map onto the gold shape — or decline."""
        from app.services.chat.aggregation import SHAPE_COUNT_EVENTS
        from app.services.chat.aggregation import SHAPE_COUNT_FILES
        from app.services.chat.aggregation import SHAPE_LIST_FILES
        from app.services.chat.aggregation import SHAPE_SPEAKER_FACET
        from app.services.chat.aggregation_service import answer_aggregation
        from app.services.chat.router import route

        decision = route(query.text)
        if not decision.wants_aggregate:
            self.shapes["not-routed-to-aggregate"] += 1
            return None

        session = None
        try:
            if self.engine is not None:
                from sqlalchemy.orm import Session

                session = Session(self.engine)
            result = answer_aggregation(
                query.text,
                decision,
                db=session,
                client=self.client,
                index=self.index,
                user_id=self.user_id,
            )
        finally:
            if session is not None:
                session.close()

        if result is None:
            self.shapes["declined"] += 1
            return None
        self.shapes[result.shape] += 1

        if result.shape == SHAPE_LIST_FILES:
            return Answer.file_set(result.file_uuids)
        if result.shape == SHAPE_SPEAKER_FACET:
            if result.speaker is None:
                # A tie at the top means there is no "the most". Declining is the
                # honest outcome; naming one is a coin flip presented as a fact.
                return None
            return Answer.speaker_count(result.speaker, int(result.speaker_sessions or 0))
        if result.shape in (SHAPE_COUNT_FILES, SHAPE_COUNT_EVENTS):
            return None if result.count is None else Answer.integer(result.count)
        return None


def build_answerer(name: str, **kwargs: Any):
    """Answerer by name. Unknown names raise rather than defaulting to silence."""
    if name == "none":
        return NullAnswerer()
    if name == "reference":
        return ReferenceAnswerer(**kwargs)
    if name == "product":
        return ProductAnswerer(**kwargs)
    raise ValueError(f"Unknown answerer {name!r}; expected 'none', 'reference' or 'product'")
