#!/usr/bin/env python3
"""Signature-scoped cleanup of orphaned test data (issue #629).

Full description and usage: the ``cleanup_test_data`` package docstring (``--help`` prints it).
The implementation lives in the sibling ``cleanup_test_data`` package; this file stays the entry
point because ``release-tidy.sh`` and ``cleanup-test-leftovers.sh`` call it by path, and the unit
tests load it by path and read the names re-exported below.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

#: package module -> the names it defines, re-exported here so a path-loaded copy of this script
#: exposes the same attributes it did before the split.
_EXPORTS = {
    'settings': (
        '_ARTIFACT_SWEEP_MODULE',
        '_CLEANUP_USERS_SCRIPT',
        '_REPO_ROOT',
        '_admin_email',
        '_admin_password',
        '_backend_url',
        '_engine_url',
        '_env',
        '_host_setting',
        '_load_artifact_sweep',
        '_load_cleanup_users_module',
        '_resolve_port',
        '_setting',
    ),
    'cutoff': (
        '_STARTED_AT_RE',
        '_TESTRUNS_DIR',
        '_read_started_at',
        'apply_age_floor',
        'default_testruns_dir',
        'live_marker_start_times',
        'prune_stale_markers',
        'resolve_cutoff',
    ),
    'shapes': (
        'INJECTED_TEST_SEED_SHAPE',
        'LEGACY_NAME_SHAPES',
        'MEDIA_FILENAME_SPECS',
        'MediaFilenameSpec',
        'NAME_PREFIXES',
        'NAME_PREFIX_SPECS',
        '_HEX8_SHAPE',
        '_name_matches_shape',
    ),
    'finders': (
        'Candidate',
        'find_injected_test_media',
        'find_media_by_filename',
        'find_media_by_tier_a_owner',
        'find_named_resources',
    ),
    'api': (
        'ApiSession',
        '_DELETE_PATH_BY_RESOURCE',
        'describe_failure',
    ),
    'owners': (
        '_delete_candidates',
        '_delete_owners',
        'find_deletable_test_owners',
        'owner_patterns',
        'owners_to_delete',
    ),
    'planes': (
        'PLANES',
        'STORE_PLANES',
        '_MAX_LINES_PER_PLANE',
        '_build_store_planes',
        '_delete_artifacts',
        '_report_orphan_profile_docs',
        '_sweep_store_planes',
        'find_orphan_profile_docs',
    ),
    'cli': (
        '_parse_planes',
        '_sweep_records',
        'build_parser',
        'main',
    ),
}


def _reexport() -> None:
    """Put the package on ``sys.path`` and copy its names into this module's namespace.

    The filename has a hyphen, so this script is run (or loaded by tests) by path rather than
    imported; add the package's parent directory explicitly instead of relying on the
    interpreter doing it.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    for module_name, names in _EXPORTS.items():
        module = importlib.import_module(f'cleanup_test_data.{module_name}')
        for name in names:
            globals()[name] = getattr(module, name)


_reexport()


if __name__ == '__main__':
    sys.exit(importlib.import_module('cleanup_test_data.cli').main())
