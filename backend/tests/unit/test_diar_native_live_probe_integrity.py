"""Guard the diar-native live probe against the two ways it silently measured nothing.

``tests/integration/test_diar_native_multigpu_provider_live.py`` is the only test that
proves a real diarization job reached the native sidecar rather than falling back to
in-process PyAnnote (issue #711). It is ``integration``/``gpu``-marked, and pyproject's
``addopts`` is ``-m 'not integration and not gpu'`` -- so **CI never runs it**, and a
regression in it would surface to nobody. These checks are unmarked and therefore do run
in the fast suite and in the GitHub ``backend-tests`` job.

Both invariants below are regressions that ACTUALLY HAPPENED and were measured live on
2026-09-05, not hypotheticals:

1. **Both output streams must be read.** ``docker logs`` replays the container's stdout and
   stderr onto the corresponding streams of the calling process, and Python's ``logging``
   writes to stderr by default. Reading only ``.stdout`` returned the empty string while
   ``otfresh-gpu711-celery-worker-gpu-scaled`` had just written five ``native diarization
   done`` lines. The dangerous half is not the missed positive assertion -- it is that
   ``"falling back to pyannote" not in ""`` is vacuously true, so the probe's fallback
   check could never have failed on any stack, in any topology, ever.

2. **The ``--since`` timestamp must be UTC-marked.** ``docker logs --since`` parses a
   timestamp carrying no offset as the DAEMON's LOCAL time. Measured on this host (EDT,
   UTC-4): ``--since 2026-09-05T22:00:00Z`` returned 5 matching lines and
   ``--since 2026-09-05T22:00:00`` returned 0, against the same container at the same
   moment. Dropping the ``Z`` shifts the window and greps an empty string -- failure mode
   (1) again, by a different route.
"""

from __future__ import annotations

import ast
from pathlib import Path

LIVE_PROBE = (
    Path(__file__).resolve().parents[1]
    / "integration"
    / "test_diar_native_multigpu_provider_live.py"
)


def _live_probe_tree() -> ast.Module:
    assert LIVE_PROBE.is_file(), (
        f"the diar-native live probe is missing from {LIVE_PROBE} -- if it was renamed, "
        "point this guard at the new path rather than deleting the guard"
    )
    return ast.parse(LIVE_PROBE.read_text(encoding="utf-8"))


def _attribute_names_read_on(tree: ast.Module, variable: str) -> set[str]:
    """Every ``<something>.<attr>`` read in an assignment to ``variable``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if variable not in targets:
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Attribute):
                found.add(sub.attr)
    return found


def test_live_probe_reads_both_stdout_and_stderr() -> None:
    """`logs` must be built from BOTH streams, or the fallback assertion is vacuous."""
    streams = _attribute_names_read_on(_live_probe_tree(), "logs")

    assert streams, (
        "found no assignment to a `logs` variable in the diar-native live probe -- this "
        "guard is keyed on that name and has gone stale; re-point it rather than deleting it"
    )
    missing = {"stdout", "stderr"} - streams
    assert not missing, (
        f"the diar-native live probe builds its `logs` string without {sorted(missing)} "
        f"(reads: {sorted(streams)}). `docker logs` puts application logging on STDERR, so "
        "reading one stream yields '' -- and `'falling back to pyannote' not in ''` passes "
        "vacuously, meaning the PyAnnote-fallback check can never fail (issue #711)."
    )


def test_live_probe_since_timestamp_is_utc_marked() -> None:
    """The `--since` format string must carry an explicit `Z`."""
    tree = _live_probe_tree()
    strftime_formats = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "strftime"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]

    assert strftime_formats, (
        "no strftime() format literal found in the diar-native live probe -- the `--since` "
        "window is what this guard protects; re-point it if the call moved"
    )
    unmarked = [fmt for fmt in strftime_formats if not fmt.endswith("Z")]
    assert not unmarked, (
        f"these strftime formats in the diar-native live probe do not end in 'Z': {unmarked}. "
        "`docker logs --since` reads an unmarked timestamp as the docker daemon's LOCAL time, "
        "which on a host west of UTC shifts the window into the future and greps an empty "
        "log (issue #711)."
    )
