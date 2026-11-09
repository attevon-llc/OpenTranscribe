"""Signature specs: the filename and name shapes a test-minted resource carries."""

from __future__ import annotations

import re
from typing import NamedTuple

# ---------------------------------------------------------------------------------------------
# Tier A candidate signatures
# ---------------------------------------------------------------------------------------------


class MediaFilenameSpec(NamedTuple):
    label: str
    sql_prefix: str  # coarse LIKE pre-filter, escaped for `ESCAPE '\'`
    shape: re.Pattern[str]  # precise shape check applied in Python


#: Coarse SQL pre-filter is intentionally loose (a bare prefix); the SHAPE regex is what
#: actually decides membership, so a human's ``reprocess-notes.wav`` is filtered out even
#: though it shares the SQL LIKE prefix with a real fixture upload.
MEDIA_FILENAME_SPECS: list[MediaFilenameSpec] = [
    MediaFilenameSpec(
        'owned', r'e2e-owned-%', re.compile(r'^e2e-owned-[0-9a-f]{8}\.[A-Za-z0-9]+$')
    ),
    MediaFilenameSpec('upload', r'e2e\_upload\_%', re.compile(r'^e2e_upload_\d+_\d+\.wav$')),
    MediaFilenameSpec(
        'reprocess', r'reprocess-%', re.compile(r'^reprocess-[0-9a-f]{8}\.[A-Za-z0-9]+$')
    ),
    MediaFilenameSpec(
        'gpu-scale-smoke',
        r'gpu-scale-smoke-%',
        re.compile(r'^gpu-scale-smoke-\d+-\d+\.wav$'),
    ),
    # integration/test_lite_mode_mocked_providers.py's per-test upload. It had no
    # backstop here at all, so an interrupted run leaked one file per test.
    MediaFilenameSpec(
        'lite-mode', r'lite-mode-test-%', re.compile(r'^lite-mode-test-[0-9a-f]{8}\.wav$')
    ),
    # integration/test_deletion_residue_live.py + test_orphan_reconcile_opensearch.py
    # (deletion_seed.DELRES_PREFIX): synthetic fully-processed files seeded straight into
    # every store. Teardown removes them; this is the backstop for a killed run.
    MediaFilenameSpec(
        'deletion-residue', r'e2e-delres-%', re.compile(r'^e2e-delres-[0-9a-f]{8}\.wav$')
    ),
    # e2e/test_full_pipeline_smoke.py's real upload (issue #1195): one unchecked DELETE was
    # its only cleanup, so a failed teardown left a fully processed file behind.
    MediaFilenameSpec(
        'pipeline-smoke', r'pipeline-smoke-%', re.compile(r'^pipeline-smoke-[0-9a-f]{8}\.wav$')
    ),
    # integration/test_diar_native_{cross_card_placement,multigpu_provider}_live.py's uploads,
    # uniquified by pid: diar-native-crosscard-<pid>.wav, diar-native-<topology>-<pid>.wav.
    MediaFilenameSpec(
        'diar-native-live',
        r'diar-native-%',
        re.compile(r'^diar-native-[a-z0-9]+(-[a-z0-9]+)*-\d+\.wav$'),
    ),
    # scripts/benchmark_parallel.py --upload (issue #1222): standalone runs used to upload the
    # corpus's REAL filenames, which no sweep can tell from a user's own recordings.
    MediaFilenameSpec(
        'bench-parallel',
        r'bench-parallel-%',
        re.compile(r'^bench-parallel-[0-9a-f]{8}\.[A-Za-z0-9]+$'),
    ),
]


#: Namespace seeds the TEST suites pass to ``corpus_injection.injector.inject_meeting``. The
#: injected file's name (``<corpus>__<meeting>.transcript``) is the corpus's, not ours, so these
#: files are recognised by the ``rag_eval.seed`` the injector stamps on them instead. FULL
#: match with the uuid4 uniquifier: the canonical (empty) seed and any operator-chosen seed
#: (``run1``, ``rag403``) are real eval corpora and are never a candidate. Producers:
#: ``integration/test_corpus_injection{,_synthetic_e2e,_purge}.py`` (``pytest-<hex12>``,
#: ``pytest-synth-<hex10>``), ``fixtures/search_corpus_stack.py`` (``pytest-searchqual-<hex10>``)
#: and ``e2e/owned_corpus.py`` (``e2e-owned-<hex10>``).
INJECTED_TEST_SEED_SHAPE = re.compile(r'(pytest(-synth|-searchqual)?|e2e-owned)-[0-9a-f]{10,12}')


#: name/title-prefix + <8hex> shape, one entry per non-media resource type.
NAME_PREFIX_SPECS: dict[str, tuple[str, str]] = {
    # label: (table, column)
    'collection': ('collection', 'name'),
    'tag': ('tag', 'name'),
    'watch_source': ('watch_source', 'name'),
    'speaker_profile': ('speaker_profile', 'name'),
    'chat_conversation': ('chat_conversation', 'title'),
    # Clusters a test's speakers form (deletion_seed's `e2e-delres-<hex8>[-shared]`, a profile-
    # backed cluster labelled after an e2e profile). A profile delete only NULLs the cluster's
    # promoted_to_profile_id, and an empty promoted cluster is not reaped, so these outlive the
    # test that made them (issue #1195). Deleted through the API: it also drops the centroid.
    'speaker_cluster': ('speaker_cluster', 'label'),
}


NAME_PREFIXES: dict[str, list[str]] = {
    'collection': ['e2e-shared-', 'e2e-collection-', 'e2e-delres-'],
    'tag': ['e2e-tag-', 'e2e-delres-'],
    'watch_source': ['e2e-watch-', 'e2e-delres-'],
    'speaker_profile': ['e2e-gender-', 'e2e-delres-', 'e2e-propagated-'],
    # probe-: scripts/probe_chat_rag.py's per-run 'probe-<hex8>: <label>' titles (issue #1195).
    'chat_conversation': ['e2e-chat-', 'e2e-delres-', 'probe-'],
    'speaker_cluster': ['e2e-gender-', 'e2e-propagated-', 'e2e-delres-'],
}


_HEX8_SHAPE = re.compile(r'[0-9a-f]{8}')


#: Names tests minted BEFORE they were renamed to a registered lowercase prefix. Kept so a
#: leftover already sitting on a stack is still swept; nothing may mint these any more
#: (test_cleanup_test_data_safety.py's e2e literal gate refuses an unregistered head). FULL
#: match, uniquifier included, so a human's "E2E Propagated notes" is never one.
LEGACY_NAME_SHAPES: dict[str, list[re.Pattern[str]]] = {
    # test_speaker_gender_clusters.py's rename target until 2026-10-11 (issue #1195): the
    # rename creates a speaker profile the test never deleted. Seen on the v060 stack.
    'speaker_profile': [re.compile(r'E2E Propagated [0-9a-f]{8}')],
}


def _name_matches_shape(name: str, prefixes: list[str], resource: str | None = None) -> bool:
    """A registered prefix followed somewhere by an 8-hex-char uniquifier — never a
    bare prefix match, so a human-created ``e2e-tag-notes`` (no hex suffix) survives.
    ``resource`` additionally admits that resource's FULL-match legacy shapes.
    """
    for prefix in prefixes:
        if name.startswith(prefix) and _HEX8_SHAPE.search(name[len(prefix) :]):
            return True
    return any(shape.fullmatch(name) for shape in LEGACY_NAME_SHAPES.get(resource or '', []))
