"""The speaker list written into every chunk must not depend on set iteration order.

``list(set(...))`` over strings is order-unstable **across processes**: CPython
randomises string hashing per interpreter unless ``PYTHONHASHSEED`` is pinned,
and it is pinned nowhere in this repo — no compose file, no Dockerfile, no
entrypoint sets it.

That list is written into every chunk document, so an unsorted one means the
same transcript indexes to different content — and therefore different
embeddings and different hybrid scores — depending on which Celery worker
process happened to pick up the task. It is invisible within one process and on
a single-worker box, which is why normal use never surfaced it and a
re-indexing benchmark did.

Sorting costs nothing: neither caller depends on the order (both pass the list
straight into the indexed document), and a stable order is what makes
re-indexing reproducible.

**Why this test is in-process rather than spawning interpreters.** The honest
test of a cross-process property is to run several interpreters with different
``PYTHONHASHSEED`` values and compare — but importing this module pulls in the
app, so four subprocesses took minutes and timed out. Asserting the output *is*
sorted is equivalent in practice: any regression to ``list(set(...))`` would
have to produce an already-sorted permutation of eight names to slip through,
which is 1 in 40,320 per run.
"""

from __future__ import annotations

from app.tasks.transcription.storage import get_unique_speaker_names

# Eight names, deliberately not in insertion order and not alphabetical, so a
# regression cannot pass by coincidence of the input already being sorted.
SEGMENTS = [
    {"speaker": "Zara"},
    {"speaker": "SPEAKER_02"},
    {"speaker": "Dana"},
    {"speaker": "ravi"},
    {"speaker": "Mia"},
    {"speaker": "SPEAKER_01"},
    {"speaker": "Ana"},
    {"speaker": "Dana"},  # duplicate: the function also de-duplicates
]


def test_speaker_names_are_deduplicated_and_sorted():
    assert get_unique_speaker_names(SEGMENTS) == [
        "Ana",
        "Dana",
        "Mia",
        "SPEAKER_01",
        "SPEAKER_02",
        "Zara",
        "ravi",
    ]


def test_the_order_is_a_function_of_the_names_not_of_iteration():
    """Shuffling the input must not move the output.

    This is the property the indexer relies on: two workers handed the same
    segments in different orders must write the same document.
    """
    reversed_input = list(reversed(SEGMENTS))
    assert get_unique_speaker_names(reversed_input) == get_unique_speaker_names(SEGMENTS)


def test_sorting_is_by_codepoint_so_it_cannot_drift_with_locale():
    """Guard the guard: pin *which* order, not merely that one exists.

    ``sorted()`` on str is codepoint order, so uppercase precedes lowercase —
    'ravi' sorts last, after 'Zara'. Asserting this explicitly means a future
    switch to a locale- or case-insensitive sort is a visible change to the
    indexed document rather than a silent one.
    """
    names = get_unique_speaker_names(SEGMENTS)
    assert names == sorted(names)
    assert names[-1] == "ravi", "lowercase must sort after uppercase (codepoint order)"
