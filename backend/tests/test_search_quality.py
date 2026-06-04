"""Search quality integration tests against live OpenSearch data.

Run: pytest backend/tests/test_search_quality.py -v
Requires: running OpenTranscribe with indexed data (20 files, 1943 chunks)

These tests validate search behavior against a real dataset:
- Joe Rogan #2219 - Donald Trump (630 chunks)
- Joe Rogan #2221 - JD Vance (545 chunks)
- Secret Airships (102 chunks)
- Apple Event September 9 (96 chunks)
- DOGE's Findings (65 chunks)
- Pyramids & Sahara (57 chunks)
- AI Arms Race with China (54 chunks)
- Apollo 11 (47 chunks)
- Scam Factories (46 chunks)
- And more (20 files total)

NOTE: These tests require a running OpenTranscribe server with specific indexed data.
They are skipped by default. Set RUN_SEARCH_QUALITY_TESTS=true to run them.
"""

import os
import re

import pytest
import requests

# Skip all tests - requires live server with specific indexed data
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SEARCH_QUALITY_TESTS", "false").lower() != "true",
    reason="Search quality tests require live server with indexed data (set RUN_SEARCH_QUALITY_TESTS=true to run)",
)

BASE = "http://localhost:5174/api"


@pytest.fixture(scope="module")
def auth_token():
    """Login once per module, retrying through transient auth rate limiting."""
    import time

    last_status = None
    for attempt in range(4):
        resp = requests.post(
            f"{BASE}/auth/login",
            data={"username": "admin@example.com", "password": "password"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()["access_token"]
        last_status = resp.status_code
        time.sleep(5 * (attempt + 1))
    pytest.skip(f"Cannot authenticate against dev stack (HTTP {last_status})")


@pytest.fixture(scope="module")
def headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


def search(headers, q, mode="hybrid", sort="relevance"):
    resp = requests.get(
        f"{BASE}/search",
        params={"q": q, "search_mode": mode, "sort_by": sort, "page_size": 20},
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# The relevance pins below were calibrated against a specific ~20-file
# dataset (see module docstring). "Target file must appear in the top 20"
# is only a meaningful signal at that scale.
PINNED_CORPUS_MAX_FILES = 100


def require_corpus(headers, title_substring: str) -> None:
    """Skip the calling test unless the pinned corpus is in place.

    Two conditions: the target file must be indexed, AND the library must be
    near the pinned dataset's scale — on a multi-thousand-file corpus a
    specific small file missing the top-20 is expected, not a regression.
    """
    resp = requests.get(
        f"{BASE}/files", params={"page": 1, "page_size": 1}, headers=headers, timeout=10
    )
    resp.raise_for_status()
    total = resp.json().get("total", 0)
    if total > PINNED_CORPUS_MAX_FILES:
        pytest.skip(
            f"Library has {total} files — relevance pins are calibrated for the "
            f"~20-file dataset (max {PINNED_CORPUS_MAX_FILES})"
        )

    data = search(headers, title_substring, mode="keyword")
    titles = [r["title"] for r in data.get("results", [])]
    if not any(title_substring.lower() in t.lower() for t in titles):
        pytest.skip(f"Corpus file matching {title_substring!r} not indexed in this environment")


class TestNeuralSearchHealth:
    """Corpus-independent: the semantic half of hybrid search must be alive.

    Guards against silent neural degradation — a DEPLOY_FAILED embedding
    model makes every hybrid query quietly fall back to BM25-only (found
    in exactly that state on the dev cluster, June 2026).
    """

    def test_active_neural_model_is_deployed(self, headers):
        resp = requests.get(f"{BASE}/search/models/neural", headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("neural_enabled"):
            pytest.skip("Neural search disabled in this environment")
        assert data.get("active_model_id"), "Neural search enabled but no active model"
        active = [m for m in data["models"] if m.get("model_id") == data["active_model_id"]]
        assert active, f"Active model id not in model list: {data['active_model_id']}"
        state = active[0].get("model_state") or active[0].get("state")
        assert state == "DEPLOYED", (
            f"Active neural model is {state!r}, not DEPLOYED — semantic search "
            "is silently degraded to BM25-only"
        )


# ── Semantic Suppression Tests ──────────────────────────────


class TestSemanticSuppression:
    """When keyword matches exist, irrelevant semantic results must be suppressed."""

    def test_china_suppresses_airships_from_keyword(self, headers):
        """'china' must NOT return Secret Airships as a keyword match."""
        data = search(headers, "china")
        kw_titles = [r["title"] for r in data["results"] if not r["semantic_only"]]
        for t in kw_titles:
            assert "Airship" not in t, f"Airships wrongly in keyword results: {t}"

    def test_china_keyword_files_present(self, headers):
        """'china' must return files: AI Arms Race, Palmer Luckey, etc."""
        require_corpus(headers, "AI Arms Race")
        data = search(headers, "china")
        kw = [r for r in data["results"] if not r["semantic_only"]]
        kw_titles = [r["title"] for r in kw]
        assert any("AI Arms Race" in t or "China" in t for t in kw_titles), (
            f"Missing China-related keyword files: {kw_titles}"
        )
        assert len(kw) >= 5, f"Expected >= 5 keyword files for 'china', got {len(kw)}"

    def test_fight_suppresses_irrelevant(self, headers):
        """'fight' must NOT return Bridge to Space."""
        data = search(headers, "fight")
        sem_titles = [r["title"] for r in data["results"] if r["semantic_only"]]
        for t in sem_titles:
            assert "Bridge" not in t, f"Irrelevant semantic result for 'fight': {t}"

    def test_nasa_returns_relevant(self, headers):
        """'nasa' must return Apollo 11, NASA Spy Agency, Bridge to Space, Warp Drive."""
        data = search(headers, "NASA")
        kw_titles = [r["title"] for r in data["results"] if not r["semantic_only"]]
        assert any("Apollo" in t or "Eagle" in t or "NASA" in t for t in kw_titles), (
            f"Missing NASA-related files: {kw_titles}"
        )


# ── Exact Mode Precision Tests ──────────────────────────────


class TestExactMode:
    """Keyword/exact mode must match only exact word forms."""

    def test_fight_no_stem_highlights(self, headers):
        """'fight' exact must only highlight fight/fights/fighting, not right/might/eight."""
        data = search(headers, "fight", mode="keyword")
        for r in data["results"]:
            for occ in r["occurrences"]:
                marks = re.findall(r"<mark>(.*?)</mark>", occ["snippet"])
                for m in marks:
                    assert "fight" in m.lower(), f"Stem false positive: '{m}'"

    def test_spy_exact_precision(self, headers):
        """'spy' exact must match only spy-related content."""
        data = search(headers, "spy", mode="keyword")
        for r in data["results"]:
            assert r["keyword_occurrences"] > 0

    def test_keyword_mode_no_semantic(self, headers):
        """Keyword mode must never return semantic-only results."""
        for q in ["fight", "china", "NASA", "Trump", "fraud"]:
            data = search(headers, q, mode="keyword")
            for r in data["results"]:
                assert not r["semantic_only"], (
                    f"'{q}' keyword mode returned semantic result: {r['title']}"
                )


# ── Match Count Accuracy Tests ──────────────────────────────


class TestMatchCounts:
    """Match counts must reflect actual keyword matches, not semantic noise."""

    def test_keyword_files_positive_count(self, headers):
        """All keyword-matched files must have keyword_occurrences > 0."""
        for q in ["china", "fight", "Trump", "NASA"]:
            data = search(headers, q)
            for r in data["results"]:
                if not r["semantic_only"]:
                    assert r["keyword_occurrences"] > 0, (
                        f"'{q}': {r['title']} has kw_occ=0 but isn't semantic_only"
                    )

    def test_semantic_files_zero_keyword_count(self, headers):
        """Semantic-only files must have keyword_occurrences == 0."""
        data = search(headers, "geopolitics")
        for r in data["results"]:
            if r["semantic_only"]:
                assert r["keyword_occurrences"] == 0


# ── Speaker & Metadata Tests ────────────────────────────────


class TestSpeakerSearch:
    """Speaker name searches must detect metadata speaker presence."""

    def test_joe_rogan_metadata_speaker(self, headers):
        """All files with Joe Rogan as speaker must have metadata_speaker source."""
        data = search(headers, "Joe Rogan")
        for r in data["results"]:
            if "Joe Rogan" in r.get("speakers", []):
                assert "metadata_speaker" in r["match_sources"], (
                    f"Missing metadata_speaker: {r['title']}, src={r['match_sources']}"
                )

    @pytest.mark.xfail(
        reason="Known defect (issue #234): files whose chunks carry matching "
        "speaker^3 metadata flood the RRF over-fetch window, so hybrid+collapse "
        "returns a single group for speaker-name queries. Needs window "
        "diversification (OpenSearch 3.4 forbids aggs with hybrid+collapse+RRF).",
        strict=False,
    )
    def test_speaker_search_finds_files(self, headers):
        """Searching speaker name must return files they appear in."""
        data = search(headers, "Joe Rogan")
        assert data["total_files"] >= 10, (
            f"Joe Rogan is in 15+ files but search found {data['total_files']}"
        )

    def test_trump_in_title_and_content(self, headers):
        """'Trump' should match title and content sources."""
        require_corpus(headers, "Donald Trump")
        data = search(headers, "Trump")
        trump_file = next((r for r in data["results"] if "Donald Trump" in r["title"]), None)
        assert trump_file is not None, "Trump file not found"
        assert "title" in trump_file["match_sources"] or "content" in trump_file["match_sources"]


# ── Highlight Type Tests ────────────────────────────────────


class TestHighlightType:
    """Occurrences must have correct highlight_type for styling."""

    def test_keyword_type(self, headers):
        """Keyword-matched files must have at least one keyword-type occurrence."""
        data = search(headers, "china")
        for r in data["results"]:
            if not r["semantic_only"]:
                types = {occ.get("highlight_type") for occ in r["occurrences"]}
                assert "keyword" in types, (
                    f"Keyword file '{r['title']}' has no keyword highlights: {types}"
                )

    def test_semantic_type(self, headers):
        """Semantic-only occurrences must have highlight_type='semantic'."""
        data = search(headers, "international relations between superpowers")
        for r in data["results"]:
            if r["semantic_only"]:
                for occ in r["occurrences"]:
                    assert occ.get("highlight_type") == "semantic"


# ── Relevance Ordering Tests ───────────────────────────────


class TestRelevanceOrder:
    """Keyword matches must always rank above semantic-only results."""

    def test_keyword_before_semantic(self, headers):
        """When keyword matches exist, the TOP result must be a keyword match.

        The March 2026 hybrid overhaul deliberately replaced hard suppression
        with SOFT demotion: strong semantic hits may interleave below the top
        keyword results, so strict "all keyword before all semantic" ordering
        no longer holds by design. The invariant that remains is that a
        semantic-only hit never outranks every keyword match.
        """
        for q in ["china", "fight", "NASA", "fraud"]:
            data = search(headers, q)
            results = data["results"]
            if not results or all(r["semantic_only"] for r in results):
                continue  # no keyword matches for this corpus/query
            assert not results[0]["semantic_only"], (
                f"'{q}': semantic-only result ranked above every keyword match: "
                f"{results[0]['title']}"
            )


# ── Semantic Search Quality Tests ───────────────────────────


class TestSemanticQuality:
    """Semantic search should find topically related content."""

    def test_espionage_finds_spy_content(self, headers):
        """'espionage' should find NASA Spy Agency and surveillance content."""
        require_corpus(headers, "Spy")
        data = search(headers, "espionage")
        titles = [r["title"] for r in data["results"]]
        assert any("spy" in t.lower() or "nasa" in t.lower() for t in titles), (
            f"Espionage should find spy/NASA content: {titles}"
        )

    def test_artificial_intelligence_finds_ai(self, headers):
        """'artificial intelligence' should find AI Arms Race, Warp Drive AI, etc."""
        require_corpus(headers, "AI Arms Race")
        data = search(headers, "artificial intelligence")
        titles = [r["title"] for r in data["results"]]
        assert any("AI" in t for t in titles), (
            f"'artificial intelligence' should find AI content: {titles}"
        )

    def test_cryptocurrency_fraud_finds_scam(self, headers):
        """'cryptocurrency fraud' should find Scam Factories."""
        require_corpus(headers, "Scam")
        data = search(headers, "online fraud scam")
        titles = [r["title"] for r in data["results"]]
        assert any("Scam" in t for t in titles), f"Fraud search should find scam content: {titles}"

    def test_space_exploration_finds_nasa(self, headers):
        """'space exploration' should find NASA, Apollo, Bridge to Space."""
        require_corpus(headers, "Apollo")
        data = search(headers, "space exploration")
        titles = [r["title"] for r in data["results"]]
        space_matches = [
            t for t in titles if any(w in t for w in ["Space", "Apollo", "Eagle", "NASA", "Warp"])
        ]
        assert len(space_matches) >= 2, f"Space exploration should find multiple matches: {titles}"

    def test_government_corruption_finds_pelosi(self, headers):
        """'government corruption' should find Nancy Pelosi insider trading."""
        require_corpus(headers, "Pelosi")
        data = search(headers, "government corruption insider trading")
        titles = [r["title"] for r in data["results"]]
        assert any("Pelosi" in t or "insider" in t.lower() for t in titles), (
            f"Corruption search should find Pelosi: {titles}"
        )

    def test_archaeology_finds_pyramids(self, headers):
        """'ancient archaeology discoveries' should find pyramid content."""
        require_corpus(headers, "Pyramids")
        data = search(headers, "ancient archaeology discoveries")
        titles = [r["title"] for r in data["results"]]
        assert any("Pyramid" in t or "Sahara" in t for t in titles), (
            f"Archaeology should find pyramids: {titles}"
        )


# ── Multi-word and Phrase Tests ─────────────────────────────


class TestPhraseSearch:
    """Multi-word searches should match phrases correctly."""

    @pytest.mark.xfail(
        reason="Known defect (issue #234): when exactly one file has labeled "
        "speakers, its per-chunk speaker^3 metadata matches flood the RRF "
        "over-fetch window and collapse returns a single group. Passes on "
        "uniformly-labeled corpora; needs window diversification to fix "
        "(OpenSearch 3.4 forbids aggs with hybrid+collapse+RRF).",
        strict=False,
    )
    def test_joe_rogan_experience(self, headers):
        """'Joe Rogan Experience' should match title and content."""
        data = search(headers, "Joe Rogan Experience")
        assert data["total_files"] >= 5

    def test_warp_drive(self, headers):
        """'warp drive' should find Warp Drive article."""
        require_corpus(headers, "Warp Drive")
        data = search(headers, "warp drive")
        kw_titles = [r["title"] for r in data["results"] if not r["semantic_only"]]
        assert any("Warp" in t for t in kw_titles), f"Missing warp drive: {kw_titles}"

    def test_quantum_computer(self, headers):
        """'quantum computer' should find China Quantum Computer."""
        require_corpus(headers, "Quantum")
        data = search(headers, "quantum computer")
        kw_titles = [r["title"] for r in data["results"] if not r["semantic_only"]]
        assert any("Quantum" in t for t in kw_titles), f"Missing quantum: {kw_titles}"


# ── Speaker-Scoped Search Tests ─────────────────────────────


class TestSpeakerScopedSearch:
    """speaker: operator must filter by speaker."""

    def test_speaker_operator_basic(self, headers):
        """'speaker:"Joe Rogan" china' should only return Joe Rogan's chunks."""
        data = search(headers, 'speaker:"Joe Rogan" china')
        for r in data["results"]:
            for occ in r["occurrences"]:
                assert occ["speaker"] == "Joe Rogan", (
                    f"Wrong speaker: {occ['speaker']} (expected Joe Rogan)"
                )

    def test_speaker_operator_filters_occurrences(self, headers):
        """Speaker-scoped search must only return occurrences from that speaker."""
        scoped = search(headers, 'speaker:"Joe Rogan" china')
        for r in scoped["results"]:
            for occ in r["occurrences"]:
                assert occ["speaker"] == "Joe Rogan", (
                    f"Scoped search returned wrong speaker: {occ['speaker']}"
                )

    def test_speaker_only_returns_all_content(self, headers):
        """Just 'speaker:"Joe Rogan"' should return all files with that speaker."""
        data = search(headers, 'speaker:"Joe Rogan"')
        assert data["total_files"] >= 10
