"""Where an aggregation answer comes from — never from a language model.

#403 is explicit: the aggregation class is answered from **OpenSearch
aggregations or Postgres**, never by an LLM counting, and **D6 makes the no-LLM
deployment first-class**. An answer-scoring path that needed a model would
contradict the property it exists to measure, so nothing in the first three
answerers below imports one.

``RagAnswerer`` (#463, added below) is deliberately the exception, not a
contradiction of the rule above: it answers a DIFFERENT ``scored_on``
(``answer_text``, free-text QMSum queries — see ``harness/answer_text.py``,
``harness/answer_judge.py`` and ``harness/faithfulness_judge.py``), never the
aggregation class, and D6 is exactly why
it exists — measuring answer QUALITY needs an actual generation to score, the
same way #403's aggregation measurement needed a real count. Nothing about D6
says "never build the instrument that measures the LLM-optional deployment's
LLM path" — it says a deployment WITHOUT an LLM must still be a first-class,
fully-functional product, which #463's floor tier (``answer_text.py`` — ROUGE,
token-F1, no model) already guarantees independently of this answerer.

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

    def _session_factory(self):
        """Open one short-lived session per call, closed on exit of the block."""
        import contextlib

        from sqlalchemy.orm import Session

        @contextlib.contextmanager
        def _scope():
            session = Session(self.engine)
            try:
                yield session
            finally:
                session.close()

        return _scope

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
            # Superseded by v390 (#403 R7). Kept as a *field* rather than deleted because
            # what it records is the scope of the claim the number supports, and that is
            # still narrower than "date questions work" — see below.
            "known_limitation": (
                "the temporal filter resolves media_file.recorded_date in Postgres (v390), "
                "not upload_time, so date-scoped questions are answered on when the meeting "
                "happened. What this corpus does NOT measure: recorded_date is written here "
                "by the injector from the corpus record — the analogue of a container "
                "creation_time — because these meetings encode their date in neither their "
                "title, their filename, nor their dialogue. The filename and transcript "
                "extractors are therefore UNEXERCISED by this score and rest entirely on "
                "their unit tests. A file whose date no source knows is excluded from the "
                "filter and counted in coverage.undated_files_excluded, so a count here is "
                "a floor over any corpus that is not fully dated."
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

        result = answer_aggregation(
            query.text,
            decision,
            # A factory, not a session: `answer_aggregation` opens a short one per
            # Postgres statement group so its OpenSearch search never inherits an
            # open transaction. `None` = no engine, and those shapes decline.
            session_factory=self._session_factory() if self.engine is not None else None,
            client=self.client,
            index=self.index,
            user_id=self.user_id,
        )

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


@dataclass(frozen=True)
class RagAnswer:
    """One generated answer, plus the context it was generated from.

    ``contexts`` is the masked excerpt TEXT that actually reached the prompt —
    exactly what ``faithfulness_judge.score_faithfulness_one`` needs as
    ``retrieved_contexts``, and exactly why this is a separate return type from
    the other answerers' bare ``Answer | None``: an ``answer_text``-scored query
    is judged on two different things (the text itself, against
    ``gold_text``; and the text against ITS OWN context, for faithfulness), and
    only one of those is derivable from a bare string.
    """

    text: str
    contexts: list[str]
    excerpt_ids: list[int]
    retrieved: int
    reranked: int


class RagAnswerer:
    """The product's REAL chat retrieve -> prompt -> generate path, driven in-process (#463).

    Unlike ``NullAnswerer``/``ReferenceAnswerer``/``ProductAnswerer`` above, this drives an
    actual LLM generation — it exists to measure ANSWER QUALITY (#463), not to answer the
    never-LLM aggregation class those three are scoped to (see module docstring).

    ⚠️ **The single most important correctness constraint in this class**: ``rerank_enabled``
    is passed to ``retrieve_context`` DIRECTLY, never through
    ``chat.settings.apply_user_preferences``. That function's ``rerank_enabled`` is
    **one-way narrowing** — ``base.rerank_enabled and rerank_enabled`` — so if the resolved
    admin default happens to be ``False``, asking for ``rerank_enabled=True`` through it
    would silently produce ``False`` anyway. An A/B that needs its "on" arm to reliably BE on
    (#463's own reranker comparison depends on this) cannot go through that function at all.
    This class instead constructs its own ``ChatSettings`` with the caller's exact
    ``rerank_enabled`` value written in, bypassing the narrowing entirely.

    Requires an explicit, working OpenAI-compatible provider (``base_url`` + ``model``) —
    **hard-fails at construction** with a clear message when none is configured, rather than
    silently producing empty answers that would read as "the system declined to answer" in a
    results file instead of "this measurement never ran".

    Args:
        client: OpenSearch client.
        index: Chunk index name.
        user_id: Owner of the corpus.
        session_factory: Callable returning a session context manager, for
            ``mask_chunks``'s two-phase gather/mask (``chat/redactor.py``). Required —
            unlike the aggregation answerers' ``engine: Any | None``, there is no "decline
            without Postgres" mode here: masking fails closed with NO session, which would
            silently send unmasked chunk text to the LLM on every deployment that redacts.
        base_url: The OpenAI-compatible server's base URL.
        model: Model name as the server names it.
        api_key: Bearer token, or a placeholder for a server that does not check one.
        rerank_enabled: Passed directly to ``ChatSettings`` — see the class docstring.
        search_mode: ``hybrid`` | ``semantic`` | ``keyword``.
        candidate_pool: Retrieval candidate pool size before reranking.
        final_chunks: Excerpts that reach the prompt.
        max_chunks_per_file: Ceiling on chunks contributed by one recording.
        rerank_max_pairs: Pairs the cross-encoder scores.
        context_window: The provider's context window, for the excerpt budget calc.
        response_tokens: Tokens reserved for the answer AND the provider's own
            completion budget — the default (2048) is set generously because
            ``gemma-4-e4b`` on the reference vLLM has NO reasoning off-switch
            (measured: `app/services/CLAUDE.md`'s reasoning table — ``false`` is
            byte-identical to omitting the kwarg, ~931-1656 reasoning tokens are
            produced regardless) and reasoning tokens are drawn from the SAME
            ``max_tokens`` budget as the final answer content. Verified directly
            against the live vLLM while this class was written: ``max_tokens=10``
            truncated mid-reasoning and returned NO answer content at all;
            ``max_tokens=200`` was enough for a trivial question's ~47-token
            reasoning trace. A real QMSum answer's reasoning trace is unmeasured
            and could be larger — this default is a starting point, not a
            calibrated floor.
        temperature: Sampling temperature. **0.0 by default** — an answer-quality
            measurement that varied run to run at nonzero temperature would not be
            reproducible, the same reason ``answer_judge.JUDGE_TEMPERATURE`` is pinned.
        unmask_for_local: Forwarded to ``mask_chunks`` — whether the provider counts as
            local under ``redaction/llm_guard.is_local_provider`` (see that module's
            CLAUDE.md entry). Pass explicitly rather than re-deriving it here, so a caller
            who already knows the provider is local (e.g. a fixed vLLM base URL under
            their control) does not pay a DNS-classification call per query.
    """

    name = "rag"

    def __init__(
        self,
        client: Any,
        index: str,
        user_id: int,
        session_factory: Any,
        *,
        base_url: str,
        model: str,
        api_key: str = "not-needed",
        rerank_enabled: bool = True,
        search_mode: str = "hybrid",
        candidate_pool: int = 48,
        final_chunks: int = 40,
        max_chunks_per_file: int = 12,
        rerank_max_pairs: int = 48,
        context_window: int = 8192,
        response_tokens: int = 2048,
        temperature: float = 0.0,
        unmask_for_local: bool = False,
    ) -> None:
        if not base_url or not model:
            raise ValueError(
                "RagAnswerer: base_url and model are both required — this answerer drives "
                "a REAL generation and must hard-fail here rather than silently producing "
                "empty answers that would read as 'declined' instead of 'never configured'."
            )
        self.client = client
        self.index = index
        self.user_id = int(user_id)
        self.session_factory = session_factory
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.rerank_enabled = bool(rerank_enabled)
        self.search_mode = search_mode
        self.candidate_pool = candidate_pool
        self.final_chunks = final_chunks
        self.max_chunks_per_file = max_chunks_per_file
        self.rerank_max_pairs = rerank_max_pairs
        self.context_window = context_window
        self.response_tokens = response_tokens
        self.temperature = temperature
        self.unmask_for_local = unmask_for_local
        self._openai_client: Any = None

    def describe(self) -> dict[str, Any]:
        return {
            "answerer": self.name,
            "summary": (
                "the PRODUCT's real retrieve -> mask -> prompt -> generate chat path, "
                "driven in-process for #463 answer-quality measurement."
            ),
            "is_production_path": True,
            "llm_required": True,
            "provider": {"base_url": self.base_url, "model": self.model},
            "rerank_enabled": self.rerank_enabled,
            "rerank_enabled_bypassed_apply_user_preferences": True,
            "temperature": self.temperature,
            "retrieval": {
                "search_mode": self.search_mode,
                "candidate_pool": self.candidate_pool,
                "final_chunks": self.final_chunks,
                "max_chunks_per_file": self.max_chunks_per_file,
                "rerank_max_pairs": self.rerank_max_pairs,
            },
        }

    def _llm_client(self) -> Any:
        if self._openai_client is None:
            from openai import OpenAI

            self._openai_client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        return self._openai_client

    def answer_with_context(self, query) -> RagAnswer | None:
        """Answer one :class:`~tests.eval.harness.corpora.EvalQuery`, with context.

        Args:
            query: an ``EvalQuery`` — ``query.spans`` is used, when present, to scope
                retrieval to the query's own gold file(s) (the "gold-file-scoped"
                general-summary queries ``corpora.load_qmsum_answer_queries`` produces);
                otherwise retrieval runs corpus-wide, matching what the product actually
                does for a real user turn.

        Returns:
            A :class:`RagAnswer`, or ``None`` if retrieval produced no usable context
            AND the model still declined to answer from general knowledge (rare, but
            possible — never silently converted to an empty string).
        """
        from app.services.chat.prompting import build_messages
        from app.services.chat.prompting import build_system_prompt
        from app.services.chat.redactor import mask_chunks
        from app.services.chat.retrieval import retrieve_context
        from app.services.chat.settings import ChatSettings

        file_uuids = sorted({span.file_uuid for span in query.spans}) or None
        settings = ChatSettings(
            candidate_pool=self.candidate_pool,
            final_chunks=self.final_chunks,
            max_chunks_per_file=self.max_chunks_per_file,
            # Bypasses apply_user_preferences deliberately -- see class docstring.
            rerank_enabled=self.rerank_enabled,
            rerank_max_pairs=self.rerank_max_pairs,
        )
        result = retrieve_context(
            query=query.text,
            user_id=self.user_id,
            organization_id=None,
            file_uuids=file_uuids,
            settings=settings,
            search_mode=self.search_mode,
        )
        masked = mask_chunks(
            self.session_factory,
            result.chunks,
            self.user_id,
            unmask_for_local=self.unmask_for_local,
        )
        system_prompt = build_system_prompt(use_context=True)
        messages, excerpt_ids = build_messages(
            system_prompt=system_prompt,
            chunks=masked,
            history=[],
            question=query.text,
            context_window=self.context_window,
            response_tokens=self.response_tokens,
        )
        contexts = [masked[i - 1].content for i in excerpt_ids if 1 <= i <= len(masked)]

        response = self._llm_client().chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            return None
        return RagAnswer(
            text=text,
            contexts=contexts,
            excerpt_ids=excerpt_ids,
            retrieved=result.retrieved,
            reranked=result.reranked,
        )

    def answer(self, query) -> str | None:
        """Text-only convenience wrapper — the shape ``answer_text.evaluate_answer_text``
        consumes. Use :meth:`answer_with_context` when faithfulness context is needed too."""
        result = self.answer_with_context(query)
        return None if result is None else result.text


def build_answerer(name: str, **kwargs: Any):
    """Answerer by name. Unknown names raise rather than defaulting to silence."""
    if name == "none":
        return NullAnswerer()
    if name == "reference":
        return ReferenceAnswerer(**kwargs)
    if name == "product":
        return ProductAnswerer(**kwargs)
    raise ValueError(f"Unknown answerer {name!r}; expected 'none', 'reference' or 'product'")
