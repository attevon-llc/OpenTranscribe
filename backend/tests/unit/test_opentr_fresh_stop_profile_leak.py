"""``stop --fresh`` must bring down profile-gated services too.

``docker compose down`` only tears down services whose profile is active for the
invocation (https://docs.docker.com/compose/how-tos/profiles/), so without
``COMPOSE_PROFILES="*"`` the ``load-monitoring`` exporters (cAdvisor keeps its
``/var/lib/docker`` mount) survive ``./opentr.sh stop --fresh <name>``.
"""

from __future__ import annotations

import re
from pathlib import Path

OPENTR_SH = Path(__file__).resolve().parents[3] / "opentr.sh"


def test_fresh_stop_down_call_enables_every_profile() -> None:
    source = OPENTR_SH.read_text(encoding="utf-8")
    match = re.search(r"^fresh_stop\(\)\s*\{(.*?)^\}\s*$", source, re.MULTILINE | re.DOTALL)
    assert match, "fresh_stop() not found in opentr.sh"
    down = re.search(r"^.*docker compose \$chain down\b.*$", match.group(1), re.MULTILINE)
    assert down, "fresh_stop() no longer runs 'docker compose $chain down'"
    assert 'COMPOSE_PROFILES="*"' in down.group(0)
