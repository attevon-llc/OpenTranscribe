"""Every worker that chunks transcripts must be able to load punkt (issue #436).

`chunking_service` splits sentences with NLTK punkt when it can and a regex
otherwise, and **the two disagree** — punkt keeps `Dr.` and `p.m.` inside a
sentence, the regex splits on them. Sentence boundaries drive chunk boundaries,
so a worker without the corpora writes different documents from one with them,
into the same index.

That was live: `index_transcript_search` routes to the `embedding` queue and
`reindex_transcripts` to `cpu`, and **neither** `celery-embedding-worker` nor
`celery-cpu-worker` mounted `nltk_data`. Confirmed inside the running
containers — both reported punkt missing while `celery-nlp-worker`, which does
no indexing, had it. So every production chunk was cut by the fallback.

This asserts the compose file rather than the container, deliberately: the
container only tells you about the stack running right now, while the compose
file is what every future deployment gets. It parses the real YAML — a grep for
"nltk_data" would pass on a mount attached to the wrong service, which is
precisely the bug.

Pairs with the per-process latch in `chunking_service` (issue #449): that makes a
worker log a WARNING when it falls back, so this condition is now noisy at
runtime as well as blocked here.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

_COMPOSE = pathlib.Path(__file__).resolve().parents[3] / "docker-compose.yml"

#: Services that run a task which calls `chunk_transcript_by_speaker_turns`.
#: Derived from `core/celery.py` task_routes: `index_transcript_search` ->
#: EMBEDDING, `reindex_transcripts` + `search_index_maintenance` -> CPU.
_CHUNKING_SERVICES = ("celery-embedding-worker", "celery-cpu-worker")

_NLTK_TARGET = "/home/appuser/.cache/nltk_data"


#: Splits a short-form volume on its separators only — colons INSIDE a
#: `${VAR:-default}` interpolation are not separators. Every mount in this file
#: is written `${MODEL_CACHE_DIR:-./models}/x:/container/path`, so a plain
#: `entry.split(":")[1]` returns `-./models}/x` and the assertion fails against a
#: perfectly correct compose file. The first version of this test did exactly
#: that and reported both services as unmounted while they were mounted.
_VOLUME_SEP_RE = re.compile(r":(?![^{]*\})")


def _volume_targets(service: dict) -> list[str]:
    """Container-side paths of a service's volumes, in either compose syntax."""
    targets = []
    for entry in service.get("volumes") or []:
        if isinstance(entry, str):
            parts = _VOLUME_SEP_RE.split(entry)
            if len(parts) >= 2:
                targets.append(parts[1])
        elif isinstance(entry, dict) and entry.get("target"):
            targets.append(str(entry["target"]))
    return targets


@pytest.fixture(scope="module")
def compose_services() -> dict:
    if not _COMPOSE.exists():  # pragma: no cover - repo layout guard
        pytest.skip(f"docker-compose.yml not found at {_COMPOSE}")
    services: dict = yaml.safe_load(_COMPOSE.read_text())["services"]
    return services


@pytest.mark.parametrize("service_name", _CHUNKING_SERVICES)
def test_a_chunking_worker_mounts_the_nltk_corpora(compose_services: dict, service_name: str):
    """A worker that writes chunks must have the same splitter as its peers."""
    service = compose_services.get(service_name)
    assert service is not None, (
        f"{service_name} is not in docker-compose.yml. If it was renamed, update "
        f"_CHUNKING_SERVICES — otherwise this test silently guards nothing."
    )

    assert _NLTK_TARGET in _volume_targets(service), (
        f"{service_name} runs transcript chunking but does not mount "
        f"{_NLTK_TARGET}. Without punkt it falls back to the regex splitter, "
        f"which disagrees with punkt on abbreviations — so the chunks it writes "
        f"will not match those written by any worker that has the corpora."
    )


def test_the_nlp_worker_still_has_it(compose_services: dict):
    """Control: the mount exists in this file and is spelled the way we expect.

    `celery-nlp-worker` had the mount throughout and is not part of the fix. If
    this fails, the assertion above is passing for some reason other than the
    mount being present — a changed cache path, or a parse that returns nothing.
    """
    assert _NLTK_TARGET in _volume_targets(compose_services["celery-nlp-worker"])


def test_the_guarded_list_is_not_empty(compose_services: dict):
    """A parametrized test over an empty list runs zero cases and passes.

    `_CHUNKING_SERVICES` is hand-maintained against `core/celery.py`'s routing.
    Emptying it — or a rename that made every lookup skip — would turn the whole
    module into a no-op with a green result.
    """
    assert _CHUNKING_SERVICES, "no services are being guarded"
    assert all(name in compose_services for name in _CHUNKING_SERVICES)
