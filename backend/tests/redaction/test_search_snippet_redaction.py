"""Search snippets must be masked for every category the user's policy masks (issue #86).

``HybridSearchService._redact_snippets`` intersected the user's categories with
``{"profanity", "custom"}`` and returned early when nothing survived. A user whose
policy masks **only** ``pii`` therefore got ``cats == set()`` and every snippet on
every search page rendered verbatim — and snippets come from ``transcript_chunks``,
which stores transcript text UNREDACTED by design. Search spans collection and group
shares, so the preview leaked to readers the transcript view would have masked it for.

Every assertion below reads the SNIPPET STRING the API would return, never an internal
flag. Against ``HEAD~`` the PII tests fail with the email address printed in the
failure message.

Four controls keep the fix from being "mask everything always":

* :func:`test_a_profanity_only_policy_never_runs_the_pii_detector` — the opt-in user
  pays for Presidio; nobody else does.
* :func:`test_a_disabled_policy_leaves_every_snippet_byte_identical`.
* :func:`test_a_pii_detector_failure_does_not_withhold_from_a_profanity_only_user` —
  the fail-closed branch stays as narrow as ``blocking_detector_failures`` says.
* :func:`test_an_excluded_pii_entity_is_not_masked` — the user's entity filter is
  still honoured, so this is a policy application and not a blanket scrub.

The detector itself is faked in most tests (a regex that returns real offsets into
whatever text it is handed, so the batching and ``<mark>`` mapping under test are
exercised for real). :func:`test_the_real_detector_masks_pii_in_a_snippet` is the one
that proves Presidio — it carries the ``models`` marker and skips without weights.
"""

from __future__ import annotations

import contextlib
import re

import pytest

from app.models.prompt import UserSetting
from app.services.redaction.spans import RedactionSpan
from app.services.search.hybrid_search_service import WITHHELD_SNIPPET
from app.services.search.hybrid_search_service import HybridSearchService
from app.services.search.hybrid_search_service import SearchHit
from app.services.search.hybrid_search_service import SearchOccurrence
from app.services.search.hybrid_search_service import SearchResponse

EMAIL = "john.smith@example.com"
PROFANITY = "damn"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


class _FakeDetector:
    """A stand-in for ``pii_presidio.detect_pii`` that counts its calls.

    Regex, not a mock return value: it computes offsets into whatever text it is
    given, which is exactly what the batching and tag-splitting code under test
    has to map back correctly. A canned span list would map back trivially and
    prove nothing.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str, words, cfg) -> list[RedactionSpan]:
        self.calls.append(text)
        spans = []
        for pattern, entity in ((_EMAIL_RE, "EMAIL"), (_SSN_RE, "SSN")):
            spans += [
                RedactionSpan(
                    char_start=m.start(),
                    char_end=m.end(),
                    category="pii",
                    entity_type=entity,
                    detector="presidio",
                    confidence=1.0,
                )
                for m in pattern.finditer(text)
            ]
        return spans


@pytest.fixture
def detector(monkeypatch) -> _FakeDetector:
    """Install the fake PII detector where ``detect_segment_spans`` resolves it."""
    from app.services.redaction.detectors import pii_presidio

    fake = _FakeDetector()
    monkeypatch.setattr(pii_presidio, "detect_pii", fake)
    return fake


@pytest.fixture
def bridged_session(db_session, monkeypatch):
    """Point ``_redact_snippets``'s own ``session_scope`` at the savepointed test session.

    The method deliberately opens its own session and closes it before any
    detector runs, so under the savepoint harness it would otherwise see none of
    the ``UserSetting`` rows these tests create.
    """

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _scope)
    return db_session


def _policy(
    db_session, user, *, enabled: bool = True, categories: str = '["pii"]', **extra
) -> None:
    rows = {"redaction_enabled": "true" if enabled else "false", "redaction_categories": categories}
    rows.update(extra)
    for key, value in rows.items():
        db_session.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.commit()


def _response(*snippets: str) -> SearchResponse:
    """A one-file search response carrying ``snippets`` as its occurrences."""
    hit = SearchHit(
        file_uuid="f0000000-0000-0000-0000-000000000086",
        file_id=86,
        title="Quarterly review",
        speakers=["Alice"],
        tags=[],
        upload_time="2026-08-14T00:00:00Z",
        language="en",
        occurrences=[
            SearchOccurrence(
                snippet=text,
                speaker="Alice",
                start_time=float(i),
                end_time=float(i) + 1.0,
                chunk_index=i,
                score=1.0,
            )
            for i, text in enumerate(snippets)
        ],
    )
    return SearchResponse(
        query="review",
        results=[hit],
        total_results=len(snippets),
        total_files=1,
        page=1,
        page_size=20,
        total_pages=1,
        search_time_ms=1.0,
    )


def _snippets(response: SearchResponse) -> list[str]:
    return [occ.snippet for hit in response.results for occ in hit.occurrences]


def _redact(response: SearchResponse, user) -> list[str]:
    HybridSearchService()._redact_snippets(response, user.id)
    return _snippets(response)


# --------------------------------------------------------------------------- the defect


def test_a_pii_only_policy_masks_the_email_in_a_snippet(bridged_session, normal_user, detector):
    """The reported defect: `enabled_categories == {"pii"}` masked nothing at all."""
    _policy(bridged_session, normal_user, categories='["pii"]')
    response = _response(f"You can reach him at {EMAIL} after the call.")

    [snippet] = _redact(response, normal_user)

    assert EMAIL not in snippet, f"PII survived in a search snippet: {snippet!r}"
    assert "[EMAIL]" in snippet
    assert detector.calls, "the PII detector was never consulted for a pii-masking user"


def test_a_mixed_policy_masks_pii_beside_the_profanity_it_already_masked(
    bridged_session, normal_user, detector
):
    """Profanity masking worked before; PII was silently dropped from the same page."""
    _policy(bridged_session, normal_user, categories='["profanity", "pii"]')
    response = _response(f"{PROFANITY.title()} it, mail {EMAIL} and cc 123-45-6789.")

    [snippet] = _redact(response, normal_user)

    assert EMAIL not in snippet, f"PII survived in a search snippet: {snippet!r}"
    assert "123-45-6789" not in snippet, f"PII survived in a search snippet: {snippet!r}"
    assert PROFANITY not in snippet.lower()
    assert "[EMAIL]" in snippet and "[SSN]" in snippet and "[PROFANITY]" in snippet


@pytest.mark.models
def test_the_real_detector_masks_pii_in_a_snippet(bridged_session, normal_user):
    """The one test that proves Presidio — not the plumbing — finds the PII.

    Everything else here fakes the detector so it can assert on offsets and call
    counts. If only those existed, a fix that wired up a detector which finds
    nothing would be indistinguishable from a working one.
    """
    pytest.importorskip("presidio_analyzer")
    from app.services.redaction.detectors import pii_presidio

    if not pii_presidio.preload():
        pytest.skip("Presidio analyzer/model unavailable")

    _policy(bridged_session, normal_user, categories='["pii"]')
    response = _response(f"Send the invoice to {EMAIL} before Friday please.")

    [snippet] = _redact(response, normal_user)

    assert EMAIL not in snippet, f"PII survived in a search snippet: {snippet!r}"
    assert "[EMAIL]" in snippet


# ------------------------------------------------------------------ <mark> tag safety


def test_mark_tags_survive_a_span_that_crosses_one(bridged_session, normal_user, detector):
    """A detected span straddling a highlight must not swallow or orphan the tag.

    OpenSearch does not wrap whole words — real output includes
    ``<mark>budget ? Not the </mark>original`` — so an entity really can begin
    inside a highlight and end outside it. Masking the raw string across the tag
    would emit an unbalanced ``</mark>`` and the frontend sanitizer then drops the
    whole fragment.
    """
    _policy(bridged_session, normal_user, categories='["pii"]')
    response = _response("Mail <mark>john.smith@</mark>example.com today")

    [snippet] = _redact(response, normal_user)

    assert "john.smith" not in snippet, f"PII survived in a search snippet: {snippet!r}"
    assert "example.com" not in snippet, f"PII survived in a search snippet: {snippet!r}"
    assert snippet.count("<mark>") == 1
    assert snippet.count("</mark>") == 1
    assert snippet.index("<mark>") < snippet.index("</mark>")


def test_a_snippet_with_nothing_to_mask_comes_back_byte_identical(
    bridged_session, normal_user, detector
):
    """Entities and highlights must survive the unescape/re-escape round trip untouched."""
    _policy(bridged_session, normal_user, categories='["pii"]')
    original = "Alice&#x27;s <mark>review</mark> of R&amp;D went &lt;well&gt;"
    response = _response(original)

    [snippet] = _redact(response, normal_user)

    assert snippet == original


# ------------------------------------------------ every snippet, not just the first


def test_every_snippet_on_the_page_is_masked(bridged_session, normal_user, detector):
    """A real page carries 94-200 snippets. Each one must be examined."""
    _policy(bridged_session, normal_user, categories='["pii"]')
    snippets = [f"Contact number {i:03d}-45-6789 was left on the shared line." for i in range(40)]
    response = _response(*snippets)

    masked = _redact(response, normal_user)

    assert len(masked) == 40
    for i, snippet in enumerate(masked):
        assert f"{i:03d}-45-6789" not in snippet, f"PII survived in snippet {i}: {snippet!r}"
        assert "[SSN]" in snippet
    assert len(detector.calls) == 40, "the detector must see each snippet as its own document"


@pytest.mark.models
def test_one_name_repeated_across_snippets_is_masked_in_every_one(bridged_session, normal_user):
    """MUST-FIRE guard against batching the page into shared ``analyze()`` calls.

    That optimisation is 2.2-3.0x faster and was written, measured and deleted:
    ``en_core_web_sm``'s NER reports each distinct ``PERSON`` **once per
    document**, so joining these three snippets yields one span and the name is
    left in clear in the other two — with a ``[NAME]`` label on the page, so the
    result looks masked. Measured through the live search API, the batched version
    leaked the name in 31 of the 32 snippets that contained it.

    Only the real detector can show this: the regex fake used elsewhere in this
    module has no per-document memory and passes under either implementation.

    ⚠️ These three sentences are the ones spaCy detects **individually** — checked,
    because it is brittle enough that a plausible fourth variant ("…reviewed the
    deck…") is not detected even on its own. A test built on that one would fail
    under both implementations and prove nothing about batching.
    """
    pytest.importorskip("presidio_analyzer")
    from app.services.redaction.detectors import pii_presidio

    if not pii_presidio.preload():
        pytest.skip("Presidio analyzer/model unavailable")

    _policy(bridged_session, normal_user, categories='["pii"]')
    response = _response(
        "Yeah — Talia Yarrow looked at it. The index-freshness is the thing driving it.",
        "Yeah — Talia Yarrow looked at it. hmm The storage-footprint is the thing driving it.",
        "Yeah — Talia Yarrow looked uh at it. The throughput is the thing driving it.",
    )

    masked = _redact(response, normal_user)

    # Assert the COUNT first. Without it every assertion below lives inside the loop,
    # so an empty `masked` — the exact shape a broken _redact would return — passes
    # silently. That is the `loop-only` finding, and it is the same "a signal that
    # cannot fire looks like a clean result" defect this module exists to prevent.
    assert len(masked) == 3, f"expected all three snippets back, got {len(masked)}"
    leaked = [i for i, s in enumerate(masked) if "Talia" in s]
    assert not leaked, f"PII survived in snippet(s) {leaked}: {[masked[i] for i in leaked]!r}"
    unlabelled = [i for i, s in enumerate(masked) if "[NAME]" not in s]
    assert not unlabelled, f"no [NAME] label in snippet(s) {unlabelled}"


# ------------------------------------------------------------------------- controls


def test_a_profanity_only_policy_never_runs_the_pii_detector(
    bridged_session, normal_user, detector
):
    """PII masking is opt-in twice over; nobody else may pay ~300-900 ms for it."""
    _policy(bridged_session, normal_user, categories='["profanity"]')
    response = _response(f"{PROFANITY.title()} it, mail {EMAIL} tomorrow.")

    [snippet] = _redact(response, normal_user)

    assert detector.calls == [], "the PII detector ran for a user who does not mask PII"
    assert PROFANITY not in snippet.lower(), "the profanity this user DOES mask survived"
    assert "[PROFANITY]" in snippet
    assert EMAIL in snippet, "PII was masked for a user whose policy does not mask it"


def test_a_disabled_policy_leaves_every_snippet_byte_identical(
    bridged_session, normal_user, detector
):
    _policy(bridged_session, normal_user, enabled=False, categories='["pii", "profanity"]')
    originals = [f"{PROFANITY.title()} it, mail {EMAIL}.", "Nothing to see <mark>here</mark>."]
    response = _response(*originals)

    assert _redact(response, normal_user) == originals
    assert detector.calls == []


def test_an_excluded_pii_entity_is_not_masked(bridged_session, normal_user, detector):
    """The user's entity filter still applies — this is policy, not a blanket scrub."""
    _policy(
        bridged_session,
        normal_user,
        categories='["pii"]',
        redaction_pii_entities='["SSN"]',
    )
    response = _response(f"Mail {EMAIL} about 123-45-6789.")

    [snippet] = _redact(response, normal_user)

    assert "123-45-6789" not in snippet, "the entity the user DOES mask survived"
    assert EMAIL in snippet, "an entity the user excluded was masked anyway"


def test_a_toxicity_only_policy_is_a_no_op_here(bridged_session, normal_user, detector):
    """The toxicity detector emits a score, never a span — there is nothing to mask."""
    _policy(bridged_session, normal_user, categories='["toxicity"]')
    original = f"{PROFANITY.title()} it, mail {EMAIL}."
    response = _response(original)

    assert _redact(response, normal_user) == [original]
    assert detector.calls == []


# ---------------------------------------------------------------------- fail closed


def _explode(*_args, **_kwargs):
    from app.services.redaction.detectors import DetectorUnavailableError

    raise DetectorUnavailableError("no Presidio on this box")


def test_a_pii_detector_failure_withholds_the_snippets_of_a_pii_masking_user(
    bridged_session, normal_user, monkeypatch
):
    """ "Could not look" and "found nothing" are the same return value — fail closed."""
    from app.services.redaction.detectors import pii_presidio

    monkeypatch.setattr(pii_presidio, "detect_pii", _explode)
    _policy(bridged_session, normal_user, categories='["pii"]')
    response = _response(f"Mail {EMAIL} today.", "Second result.")

    masked = _redact(response, normal_user)

    assert masked == [WITHHELD_SNIPPET, WITHHELD_SNIPPET]


def test_a_pii_detector_failure_does_not_withhold_from_a_profanity_only_user(
    bridged_session, normal_user, monkeypatch
):
    """``blocking_detector_failures`` keeps the withholding as narrow as the policy."""
    from app.services.redaction.detectors import pii_presidio

    monkeypatch.setattr(pii_presidio, "detect_pii", _explode)
    _policy(bridged_session, normal_user, categories='["profanity"]')
    response = _response(f"{PROFANITY.title()} it, mail {EMAIL} tomorrow.")

    [snippet] = _redact(response, normal_user)

    assert snippet != WITHHELD_SNIPPET
    assert "[PROFANITY]" in snippet
    assert EMAIL in snippet


def test_an_unresolvable_config_withholds_the_snippets(db_session, normal_user, monkeypatch):
    """Pre-existing behaviour, kept: if we cannot tell what to mask, show nothing."""

    @contextlib.contextmanager
    def _broken():
        raise RuntimeError("database is on fire")
        yield  # pragma: no cover

    monkeypatch.setattr("app.db.session_utils.session_scope", _broken)
    response = _response(f"Mail {EMAIL} today.")

    assert _redact(response, normal_user) == [WITHHELD_SNIPPET]
