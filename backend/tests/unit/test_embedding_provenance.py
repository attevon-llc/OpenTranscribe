"""The chunks index must be able to say which model produced its vectors (#437).

Switching the embedding model leaves the existing documents holding the previous
model's vectors in the same kNN space as the new ones. Cosine between two models
is not a similarity, and hybrid search ranks the two populations against each
other without complaint — so the defect is invisible unless the index records
provenance and something reads it back.

Before this module, ``embedding_model`` held the string ``"neural"`` — the
embedding **mode** — on every document. Measured on the epic's live index:
210,908 documents, cardinality **1**. A constant answers nothing.

Three things are pinned here, and each corresponds to a path that was proven able
to produce a mixed index:

- **The label written is the model, resolved from the ingest pipeline**, never
  from ``get_search_embedding_settings()``. Those two SystemSettings keys are
  written by different endpoints with nothing reconciling them, so a
  settings-derived label can name a model that never touched the vector.
- **``"neural"`` keeps meaning UNKNOWN.** It is not upgraded to a sentinel of its
  own (which would split the unknown population) and never backfilled with the
  current model (which would assert something unknowable about 210,908 documents).
- **One named model beside the unknown bucket is not "mixed".** Those documents
  might be from the same model. Firing the mixed alarm on the state every
  existing deployment enters the moment it indexes anything after this change
  would train operators to ignore the alarm.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError

from app.core.config import settings
from app.services.search import embedding_provenance as prov
from app.services.search.embedding_provenance import EMBEDDING_MODEL_ABSENT
from app.services.search.embedding_provenance import EMBEDDING_MODEL_UNKNOWN
from app.services.search.embedding_provenance import active_embedding_model
from app.services.search.embedding_provenance import reset_active_embedding_model
from app.services.search.embedding_provenance import resolve_model_label
from app.services.search.embedding_provenance import set_active_embedding_model
from app.services.search.embedding_provenance import survey_embedding_models

MODEL_A = "huggingface/sentence-transformers/all-MiniLM-L6-v2"
MODEL_B = "huggingface/sentence-transformers/msmarco-distilbert-base-tas-b"


class _StandInIndices:
    """``client.indices`` with only the one method the survey calls.

    Records the index it was asked about: surveying the wrong index would report
    a clean verdict about a corpus nobody searches.
    """

    def __init__(self, *, exists: bool = True) -> None:
        self._exists = exists
        self.probed: list[str] = []

    def exists(self, *, index: str) -> bool:
        self.probed.append(index)
        return self._exists


class _StandInSearchClient:
    """An OpenSearch client that answers the provenance aggregation from a dict.

    A real object rather than a ``Mock``: the assertions below are equality checks
    against the verdict the handler computed, so what is exercised is the
    classification rule, not the call.
    """

    def __init__(self, buckets: dict[str, int], *, exists: bool = True, raises: bool = False):
        self.indices = _StandInIndices(exists=exists)
        self._buckets = buckets
        self._raises = raises
        self.bodies: list[dict] = []
        self.searched: list[str] = []

    def search(self, *, index: str, body: dict) -> dict:
        if self._raises:
            # The measured failure of an unreachable cluster. A bare RuntimeError
            # here would pass against a handler catching the wrong thing.
            raise OpenSearchConnectionError("N/A", "connection refused", None)
        self.searched.append(index)
        self.bodies.append(body)
        return {
            "aggregations": {
                "models": {
                    "buckets": [{"key": k, "doc_count": v} for k, v in self._buckets.items()],
                    "sum_other_doc_count": 0,
                }
            }
        }


@pytest.fixture(autouse=True)
def _clean_label():
    """The label is process-global; no test may inherit another's."""
    reset_active_embedding_model()
    yield
    reset_active_embedding_model()


def _survey_with(buckets, **kwargs):
    client = _StandInSearchClient(buckets, **kwargs)
    with patch("app.services.opensearch_service.opensearch_client", client):
        return survey_embedding_models(), client


# ---------------------------------------------------------------------------
# The label: what gets stamped on a document
# ---------------------------------------------------------------------------
def test_an_unresolved_pipeline_labels_documents_unknown_not_a_model_name():
    """With no verified pipeline the label is the legacy unknown bucket.

    Anything else would attribute vectors to a model that may not have produced
    them — the specific thing 210,908 already-indexed documents make impossible
    to undo.
    """
    assert active_embedding_model() == EMBEDDING_MODEL_UNKNOWN


def test_the_label_is_the_model_the_pipeline_was_pointed_at():
    set_active_embedding_model(MODEL_A)
    assert active_embedding_model() == MODEL_A


def test_resetting_the_pipeline_drops_the_label_rather_than_leaving_it_stale():
    """A stale label is worse than none: it is a specific, believed, wrong claim."""
    set_active_embedding_model(MODEL_A)
    reset_active_embedding_model()
    assert active_embedding_model() == EMBEDDING_MODEL_UNKNOWN


def test_an_unnameable_model_falls_back_to_unknown_not_to_its_opaque_id():
    """``resolve_model_label`` returns None so the caller stores the unknown bucket.

    An ML Commons id is deployment-local and changes when the same model is
    re-registered, so labelling with it would report a mixed index where the
    vectors are identical.
    """
    service = SimpleNamespace(get_model_status=lambda _mid: {})
    with patch("app.services.search.ml_model_service.get_ml_model_service", return_value=service):
        assert resolve_model_label("some-opaque-id") is None

    set_active_embedding_model(resolve_model_label("some-opaque-id"))
    assert active_embedding_model() == EMBEDDING_MODEL_UNKNOWN


def test_resolving_a_model_yields_its_name_which_is_what_identifies_the_vectors():
    service = SimpleNamespace(get_model_status=lambda _mid: {"name": MODEL_A})
    with patch("app.services.search.ml_model_service.get_ml_model_service", return_value=service):
        assert resolve_model_label("ml-id-1") == MODEL_A


def test_resolving_without_a_model_id_asks_opensearch_nothing():
    assert resolve_model_label(None) is None
    assert resolve_model_label("") is None


# ---------------------------------------------------------------------------
# The survey: what the index says about itself
# ---------------------------------------------------------------------------
def test_two_named_models_in_one_index_is_a_proven_mixed_vector_space():
    survey, _ = _survey_with({MODEL_A: 12_000, MODEL_B: 900})

    assert survey.verdict == "mixed"
    assert survey.mixed is True
    assert survey.comparable is False
    assert survey.known_models == tuple(sorted((MODEL_A, MODEL_B)))
    assert survey.total == 12_900
    assert "MIXED VECTOR SPACE" in survey.describe()


def test_the_installed_corpus_of_neural_only_documents_is_unattributed_not_mixed():
    """The state every deployment is in today: 100% ``"neural"``, cardinality 1.

    Reporting that as mixed would fire the alarm on every existing installation
    for a condition none of them can do anything about.
    """
    survey, _ = _survey_with({EMBEDDING_MODEL_UNKNOWN: 210_908})

    assert survey.verdict == "unattributed"
    assert survey.mixed is False
    assert survey.known_models == ()
    assert survey.unattributed == 210_908


def test_one_named_model_beside_the_unknown_bucket_is_not_called_mixed():
    """The state a deployment enters the first time it indexes after this change.

    The unknown documents *might* be from the same model, so the honest verdict is
    "cannot be proven comparable", not "proven incomparable".
    """
    survey, _ = _survey_with({EMBEDDING_MODEL_UNKNOWN: 210_908, MODEL_A: 412})

    assert survey.verdict == "partially_unattributed"
    assert survey.mixed is False
    assert survey.comparable is False
    assert survey.known_models == (MODEL_A,)
    assert survey.unattributed == 210_908
    assert "full reindex" in survey.describe()


def test_a_single_model_with_no_unknown_documents_is_the_only_clean_verdict():
    survey, _ = _survey_with({MODEL_A: 5_000})

    assert survey.verdict == "uniform"
    assert survey.comparable is True
    assert survey.mixed is False


def test_documents_with_no_embedding_field_are_counted_apart_from_the_models():
    """Text-only writes have no vector at all — a different defect, not a mixed one."""
    survey, _ = _survey_with({MODEL_A: 100, EMBEDDING_MODEL_ABSENT: 7})

    assert survey.no_embedding == 7
    assert survey.known_models == (MODEL_A,)
    assert survey.verdict == "uniform"


def test_an_empty_index_is_empty_and_not_mistaken_for_agreement():
    survey, _ = _survey_with({})
    assert survey.verdict == "empty"
    assert survey.total == 0


def test_a_missing_index_reports_empty_without_searching_it():
    survey, client = _survey_with({MODEL_A: 3}, exists=False)
    assert survey.verdict == "empty"
    assert client.bodies == []
    assert client.indices.probed == [settings.OPENSEARCH_CHUNKS_INDEX]


def test_a_failed_query_reports_unavailable_and_never_reads_as_all_clear():
    """ "I could not ask" must not be indistinguishable from "everything agrees"."""
    survey, _ = _survey_with({}, raises=True)

    assert survey.verdict == "unavailable"
    assert survey.comparable is False
    assert survey.mixed is False


def test_a_defect_in_the_probe_itself_is_not_reported_as_unavailable():
    """Only cluster failures become ``unavailable``; a bug here must be loud.

    ``unavailable`` is a claim about the *cluster*. A blanket catch would let a
    defect in this function — a malformed body, a rename in the response shape —
    answer with the same verdict an unreachable cluster produces, so the one probe
    whose job is to detect a silent divergence would itself diverge silently. Only
    ``OpenSearchException`` is caught, which the measured
    ``opensearchpy.exceptions.ConnectionError`` is; a ``TypeError`` is not.
    """

    class _BrokenClient:
        indices = _StandInIndices()

        def search(self, *, index: str, body: dict) -> dict:
            raise TypeError("the probe built a body the client cannot serialize")

    with (
        patch("app.services.opensearch_service.opensearch_client", _BrokenClient()),
        pytest.raises(TypeError),
    ):
        survey_embedding_models()


def test_no_opensearch_client_reports_unavailable():
    with patch("app.services.opensearch_service.opensearch_client", None):
        assert survey_embedding_models().verdict == "unavailable"


def test_the_survey_costs_one_aggregation_and_returns_no_documents():
    """A health probe on a 210k-document index has to stay a single cheap query."""
    _, client = _survey_with({MODEL_A: 10})

    assert len(client.bodies) == 1
    # The configured chunks index, not the alias and not a default: a clean
    # verdict about the wrong index is worse than no verdict.
    assert client.searched == [settings.OPENSEARCH_CHUNKS_INDEX]
    body = client.bodies[0]
    assert body["size"] == 0
    assert list(body["aggs"]) == ["models"]
    assert body["aggs"]["models"]["terms"]["field"] == "embedding_model"
    # Documents predating the field must land in their own bucket, not vanish.
    assert body["aggs"]["models"]["terms"]["missing"] == EMBEDDING_MODEL_ABSENT


# ---------------------------------------------------------------------------
# The silent repoint: an ambiguous active model must not be guessed
# ---------------------------------------------------------------------------
class _StandInMLService:
    """``get_active_model_id``'s two collaborators, backed by lists."""

    def __init__(self, deployed: list[dict]) -> None:
        self._deployed = deployed
        #: The fallback MUST ask for deployed models only — adopting a registered
        #: but undeployed model points the pipeline at something that cannot embed.
        self.asked_deployed_only: list[bool] = []

    def get_model_status(self, model_id: str) -> dict:
        for m in self._deployed:
            if m["model_id"] == model_id:
                return {**m, "deployed": True}
        return {}

    def list_models(self, deployed_only: bool = False) -> list[dict]:
        self.asked_deployed_only.append(deployed_only)
        return list(self._deployed)


def _active_model_id(
    deployed: list[dict], stored: str | None, recorder: list | None = None
) -> str | None:
    from app.services.search.ml_model_service import OpenSearchMLModelService

    stand_in = _StandInMLService(deployed)
    if recorder is not None:
        recorder.append(stand_in)
    service = OpenSearchMLModelService.__new__(OpenSearchMLModelService)
    with (
        patch.object(OpenSearchMLModelService, "get_model_status", stand_in.get_model_status),
        patch.object(OpenSearchMLModelService, "list_models", stand_in.list_models),
        patch(
            "app.services.search.settings_service._get_setting",
            return_value=stored,
        ),
    ):
        return service.get_active_model_id()


def test_a_configured_and_deployed_model_is_returned_unchanged():
    deployed = [{"model_id": "id-a", "name": MODEL_A}]
    assert _active_model_id(deployed, stored="id-a") == "id-a"


def test_the_only_deployed_model_is_adopted_when_the_stored_one_is_gone():
    """The case the fallback exists for: the id went stale, there is no choice to make."""
    deployed = [{"model_id": "id-a", "name": MODEL_A}]
    assert _active_model_id(deployed, stored="id-vanished") == "id-a"


def test_two_deployed_models_are_refused_rather_than_picked_between():
    """The silent-repoint path: ``deployed[0]`` of an unsorted ``match_all``.

    Whatever this returns is written straight into the ingest pipeline, so an
    arbitrary pick embeds every subsequent document with a different model than
    the index already holds — with no user action and no warning. Returning None
    degrades search to BM25, which is loud and reversible.
    """
    deployed = [
        {"model_id": "id-a", "name": MODEL_A},
        {"model_id": "id-b", "name": MODEL_B},
    ]
    assert _active_model_id(deployed, stored=None) is None
    assert _active_model_id(deployed, stored="id-vanished") is None


def test_no_deployed_models_at_all_is_none():
    assert _active_model_id([], stored=None) is None


def test_the_fallback_considers_only_deployed_models():
    """Adopting a registered-but-undeployed model points the pipeline at nothing.

    ``ensure_neural_ingest_pipeline`` writes whatever this returns, so a candidate
    list that included undeployed models could repoint embedding at a model
    OpenSearch cannot run — neural search dies and, worse, the documents already
    indexed are attributed to a model that never embedded anything.
    """
    seen: list = []
    _active_model_id([{"model_id": "id-a", "name": MODEL_A}], stored=None, recorder=seen)

    assert seen, "the fallback never consulted the model list"
    assert seen[0].asked_deployed_only == [True]


# ---------------------------------------------------------------------------
# The write site: what actually lands on a document
# ---------------------------------------------------------------------------
@pytest.fixture
def indexing_seams(monkeypatch):
    """The collaborators ``index_transcript_chunks`` needs, substituted ONCE.

    Consolidated into a single fixture rather than repeated per test: what is
    under test is one line — which string lands in ``embedding_model`` — and every
    one of these seams is a network call (``get_opensearch_client``, the bulk
    load) or a second subsystem (the digest plane, the #400 tail prune) that would
    otherwise have to be reachable for a question about a string. The recorder
    exposes the real document bodies, so the assertions are about state the
    indexer produced, not about a call having happened.
    """
    from app.services.search import indexing_service

    recorder = SimpleNamespace(chunks=[], digest_metadata=[])

    for name, value in (
        ("is_neural_pipeline_available", lambda: True),
        ("ensure_chunks_index_exists", lambda: True),
        ("ensure_search_pipeline_exists", lambda: True),
        ("get_opensearch_client", lambda: object()),
    ):
        monkeypatch.setattr(indexing_service, name, value)

    # Distinct loop variables: these are bound methods taking self, whereas the
    # module-level seams above take none, and reusing `name, value` makes mypy
    # unify the two signatures into the first loop's Callable[[], object].
    service = indexing_service.TranscriptIndexingService
    for method_name, replacement in (
        (
            "_bulk_index_chunks",
            lambda self, chunks, use_neural_pipeline: (
                recorder.chunks.extend(chunks),
                len(chunks),
            )[1],
        ),
        ("_prune_stale_chunks", lambda self, file_uuid, keep_count: 0),
        (
            "_index_digest_plane",
            lambda self, **kwargs: (
                recorder.digest_metadata.append(kwargs["base_metadata"]),
                0,
            )[1],
        ),
    ):
        monkeypatch.setattr(service, method_name, replacement)

    return recorder


def _index_one(file_uuid: str) -> dict[str, Any]:
    from app.services.search.indexing_service import TranscriptIndexingService

    # index_transcript_chunks is declared `dict[str, Any] | int`. Every caller in
    # these tests reads the dict shape, so narrow with an assertion rather than a
    # cast: if the int arm ever comes back, the test says so instead of failing
    # later with an opaque subscript error.
    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=1,
        file_uuid=file_uuid,
        user_id=1,
        segments=[{"start": 0.0, "end": 4.0, "text": "hello there", "speaker": "SPEAKER_00"}],
        title="A recording",
        speakers=["SPEAKER_00"],
        tags=[],
    )
    assert isinstance(result, dict), f"expected the dict result shape, got {type(result).__name__}"
    return result


def test_the_indexer_stamps_the_resolved_model_on_every_chunk_it_writes(indexing_seams):
    """``indexing_service`` must write the model, not the mode.

    The whole defect in one assertion: before #437 this set was ``{"neural"}`` for
    every document in every index, whichever model produced the vectors.
    """
    set_active_embedding_model(MODEL_A)

    result = _index_one("11111111-1111-4111-8111-111111111111")

    assert result["chunk_count"] >= 1
    assert indexing_seams.chunks, "the bulk load received no documents"
    labels = {c["embedding_model"] for c in indexing_seams.chunks}
    assert labels == {MODEL_A}
    assert prov.EMBEDDING_MODEL_UNKNOWN not in labels


def test_the_indexer_falls_back_to_the_unknown_bucket_when_no_model_is_resolved(indexing_seams):
    """No resolvable model must reuse ``"neural"`` so unknown stays ONE bucket.

    A second sentinel would split the unattributed population and make the
    ``terms`` aggregation report two values where there is one unknown.
    """
    _index_one("22222222-2222-4222-8222-222222222222")

    assert indexing_seams.chunks, "the bulk load received no documents"
    assert {c["embedding_model"] for c in indexing_seams.chunks} == {EMBEDDING_MODEL_UNKNOWN}


def test_the_digest_plane_is_labelled_with_the_same_model_as_the_chunks(indexing_seams):
    """Both planes share one kNN space, so both need the same provenance."""
    set_active_embedding_model(MODEL_B)

    _index_one("33333333-3333-4333-8333-333333333333")

    assert indexing_seams.digest_metadata, "the digest plane was never invoked"
    assert indexing_seams.digest_metadata[0]["embedding_model"] == MODEL_B
    assert {c["embedding_model"] for c in indexing_seams.chunks} == {MODEL_B}
