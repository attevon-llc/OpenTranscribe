"""The laptop tier, as a test (deployment-tiers plan).

OpenTranscribe runs on three tiers from one codebase: a laptop, a home server, and AWS. The
laptop tier's promise is "no new REQUIRED model, no new heavy dependency" beyond what already
ships by default -- Postgres, OpenSearch, and (if the operator configures one) an LLM. This
module pins that promise structurally so a future change cannot silently break it by adding an
import, a dependency, or a compose service nobody meant to make mandatory:

- The speaker resolver (``chat/speaker_resolver.py``), recurrence detection
  (``chat/recurrence.py``), and the two aggregation modules (``chat/aggregation.py``,
  ``chat/aggregation_service.py``) run on Postgres + OpenSearch (already-required infra) and
  plain Python rules -- never an LLM call, never a new model load. Their own module docstrings
  already say so in prose; this file makes it an AST-checked invariant.
- The reranker (``chat/reranker.py``) is CPU-only by construction -- pinned by checking the
  literal ``device="cpu"`` argument the code passes to ``CrossEncoder(...)``.
- Document sidecars (docling-serve, Tika) are opt-in (``--with-documents``,
  ``docker-compose.documents.yml``), never part of the default compose chain
  (``docker-compose.yml`` + the dev override) a laptop starts with.
- No CJK/Thai segmenter dependency (jieba, sudachipy, pythainlp, ...) has been added to any
  pinned requirements file -- ``chat/speaker_resolver.py`` documents this as a deliberate
  scope boundary (script-aware candidate extraction via maximal same-script runs + a
  fuzzy-match floor, not a real segmenter).

Every detector here has a must-fire and a must-stay-clean case (issue #431's rule): a check
that matches nothing is indistinguishable from an invariant nobody broke.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import yaml

from app.services.chat import aggregation as aggregation_module
from app.services.chat import aggregation_service as aggregation_service_module
from app.services.chat import recurrence as recurrence_module
from app.services.chat import reranker as reranker_module
from app.services.chat import speaker_resolver as speaker_resolver_module

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_ROOT.parent


# --------------------------------------------------------------------------------------- #
# Helper: what a module imports, by AST -- not a grep, so a string that merely MENTIONS a
# forbidden name (a comment, a docstring like this one) can never trip it.
# --------------------------------------------------------------------------------------- #


def _imported_module_names(source: str) -> set[str]:
    """Every name a module imports: ``import x.y`` -> ``x.y``; ``from x.y import z`` ->
    ``x.y`` AND ``z`` (covers both ``from app.services import llm_service`` and
    ``from app.services.llm_service import LLMService`` shaped imports)."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            for alias in node.names:
                names.add(alias.name)
    return names


#: Substrings that mean "this module can talk to an LLM or load an embedding/rerank model".
#: None of the four Postgres+rules modules below may import anything matching one of these.
_LLM_OR_MODEL_MARKERS = (
    "llm_service",
    "llm_reasoning",
    "openai",
    "anthropic",
    "ollama",
    "vllm",
    "sentence_transformers",
    "CrossEncoder",
    "transformers",
)


def _forbidden_imports(source: str) -> set[str]:
    return {
        name
        for name in _imported_module_names(source)
        if any(marker.lower() in name.lower() for marker in _LLM_OR_MODEL_MARKERS)
    }


def test_the_forbidden_import_detector_fires_on_a_deliberately_broken_sample():
    broken_source = "from app.services.llm_service import LLMService\n"
    assert "app.services.llm_service" in _forbidden_imports(broken_source)


def test_the_forbidden_import_detector_stays_clean_on_an_unrelated_import():
    clean_source = "import re\nfrom dataclasses import dataclass\n"
    assert _forbidden_imports(clean_source) == set()


def test_speaker_resolver_has_no_llm_or_model_import():
    """``chat/speaker_resolver.py``'s own docstring: "no embedding, no LLM call, no
    OpenSearch round trip" -- Postgres + string matching only."""
    forbidden = _forbidden_imports(inspect.getsource(speaker_resolver_module))
    assert forbidden == set(), f"speaker_resolver.py imports: {sorted(forbidden)}"


def test_recurrence_has_no_llm_or_model_import():
    """``chat/recurrence.py``'s own docstring: "Pure logic only ... no LLM call" -- it reads
    plain dicts/strings its caller already fetched from Postgres."""
    forbidden = _forbidden_imports(inspect.getsource(recurrence_module))
    assert forbidden == set(), f"recurrence.py imports: {sorted(forbidden)}"


def test_aggregation_pure_tier_has_no_llm_or_model_import():
    """``chat/aggregation.py`` is the pure half of the aggregation tier -- subject
    extraction, shape choice, filter construction -- with no service calls at all."""
    forbidden = _forbidden_imports(inspect.getsource(aggregation_module))
    assert forbidden == set(), f"aggregation.py imports: {sorted(forbidden)}"


def test_aggregation_service_io_tier_has_no_llm_or_model_import():
    """``chat/aggregation_service.py`` is the I/O half -- it talks to OpenSearch (aggs) and
    Postgres, both already-required infra, but never an LLM or a loaded model."""
    forbidden = _forbidden_imports(inspect.getsource(aggregation_service_module))
    assert forbidden == set(), f"aggregation_service.py imports: {sorted(forbidden)}"


# --------------------------------------------------------------------------------------- #
# The reranker: not absent (it IS part of the laptop tier), but CPU-only by construction.
# --------------------------------------------------------------------------------------- #


def _cross_encoder_devices(source: str) -> list[str]:
    """Every literal ``device=`` value passed to a ``CrossEncoder(...)`` call in *source*."""
    tree = ast.parse(source)
    devices: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CrossEncoder"
        ):
            for kw in node.keywords:
                if kw.arg == "device" and isinstance(kw.value, ast.Constant):
                    value = kw.value.value
                    assert isinstance(value, str), (
                        f"CrossEncoder(device=...) is a non-string literal: {value!r}"
                    )
                    devices.append(value)
    return devices


def test_the_cross_encoder_device_detector_fires_on_a_deliberately_broken_sample():
    broken_source = 'model = CrossEncoder(model_name, device="cuda", max_length=512)\n'
    assert _cross_encoder_devices(broken_source) == ["cuda"]


def test_the_cross_encoder_device_detector_stays_clean_on_the_real_cpu_pin():
    clean_source = 'model = CrossEncoder(model_name, device="cpu", max_length=512)\n'
    assert _cross_encoder_devices(clean_source) == ["cpu"]


def test_reranker_cross_encoder_is_pinned_to_cpu():
    """The reranker is deliberately CPU-only and loaded in the backend container, never the
    GPU worker (see reranker.py's module docstring) -- GPU 1 is the project's only GPU and is
    reserved for transcription."""
    devices = _cross_encoder_devices(inspect.getsource(reranker_module))
    assert devices, "no CrossEncoder(...) call found at all -- detector or reranker.py moved"
    assert devices == ["cpu"] * len(devices), (
        f"CrossEncoder device kwarg(s): {devices!r} -- the reranker must stay CPU-only"
    )


# --------------------------------------------------------------------------------------- #
# No CJK/Thai segmenter dependency anywhere in the pinned requirements files.
# --------------------------------------------------------------------------------------- #

#: Real-world scriptio-continua / agglutinative segmenter packages. Matched as a substring of
#: the PyPI distribution name (lowercased), never the whole line, so a version pin or extras
#: marker can't hide one.
_CJK_SEGMENTER_PACKAGE_MARKERS = (
    "jieba",
    "sudachipy",
    "sudachidict",
    "mecab",
    "fugashi",
    "nagisa",
    "konlpy",
    "pythainlp",
    "laonlp",
    "khmer-nltk",
    "khmernltk",
    "pkuseg",
    "budoux",
    "kytea",
    "juman",
)

_REQUIREMENTS_FILES = (
    "requirements.txt",
    "requirements-nodeps.txt",
    "requirements-lite.txt",
    "requirements-ci.txt",
    "requirements-dev.txt",
    "requirements-eval.txt",
)


def _segmenter_hits(text: str) -> list[str]:
    hits: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        package = re.split(r"[=<>\[;\s]", stripped, maxsplit=1)[0].lower()
        if any(marker in package for marker in _CJK_SEGMENTER_PACKAGE_MARKERS):
            hits.append(stripped)
    return hits


def test_the_segmenter_detector_fires_on_a_deliberately_broken_sample():
    assert _segmenter_hits("numpy==1.26.0\njieba==0.42.1\n") == ["jieba==0.42.1"]


def test_the_segmenter_detector_stays_clean_on_unrelated_packages():
    assert _segmenter_hits("numpy==1.26.0\n# jieba would go here\nrequests==2.32.0\n") == []


def test_no_cjk_segmenter_dependency_in_any_pinned_requirements_file():
    hits: dict[str, list[str]] = {}
    for filename in _REQUIREMENTS_FILES:
        path = _BACKEND_ROOT / filename
        if not path.exists():
            continue
        found = _segmenter_hits(path.read_text())
        if found:
            hits[filename] = found
    assert not hits, (
        f"CJK/Thai segmenter dependency found: {hits} -- the laptop tier adds no new "
        "required dependency for script-aware speaker-mention extraction (see "
        "speaker_resolver.py's module docstring); if one is ever justified by measurement "
        "it must ship opt-in, not required"
    )


# --------------------------------------------------------------------------------------- #
# Document sidecars (docling-serve, Tika) are opt-in, not part of the default compose chain.
# --------------------------------------------------------------------------------------- #

_DOCUMENT_SIDECAR_SERVICES = frozenset({"docling-serve", "tika"})


def _compose_service_names(path: Path) -> set[str]:
    with path.open() as fh:
        parsed = yaml.safe_load(fh)
    return set((parsed or {}).get("services", {}) or {})


def test_document_sidecars_are_absent_from_the_default_compose_chain():
    """A laptop starts with ``docker-compose.yml`` (+ the auto-loaded dev override). Neither
    file may define docling-serve or tika directly -- they belong to the opt-in
    ``docker-compose.documents.yml`` overlay (``--with-documents``) only."""
    for filename in ("docker-compose.yml", "docker-compose.override.yml"):
        services = _compose_service_names(_REPO_ROOT / filename)
        present = services & _DOCUMENT_SIDECAR_SERVICES
        assert not present, f"{filename} defines opt-in document sidecar(s): {sorted(present)}"


def test_document_sidecars_do_exist_in_the_opt_in_overlay():
    """Sanity control for the test above: prove the sidecars are DEFINED somewhere (the
    opt-in overlay), so "absent from the default chain" cannot pass by the services having
    been deleted outright."""
    services = _compose_service_names(_REPO_ROOT / "docker-compose.documents.yml")
    assert services >= _DOCUMENT_SIDECAR_SERVICES
