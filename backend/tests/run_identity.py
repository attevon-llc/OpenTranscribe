"""One random token per test process, the same however the module that reads it was imported.

``tests/fixtures/*`` are registered as pytest plugins named ``fixtures.<module>`` and are also
imported by tests as ``tests.fixtures.<module>``: two module objects for one file, so a random
value drawn at a fixture module's top level differs between them. ``support_world.PASSWORD``
did, and the org-erasure cases re-authenticated with a password the plugin never hashed.
Fixture modules draw their per-run values from here instead: both copies execute the same
``from tests.run_identity import RUN_TOKEN``, which resolves to this one module.
``tests/unit/test_fixture_plugins_share_run_state.py`` enforces both halves.
"""

from __future__ import annotations

import uuid

RUN_TOKEN = uuid.uuid4().hex
