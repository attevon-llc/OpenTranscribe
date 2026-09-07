"""Flower's `/api/workers` is a one-shot startup snapshot, not a live roster (issue #609).

`gpu-scale-smoke.sh` (the Stage 2 Cycle 2B pass check) failed its very first assertion —
``no gpu-scaled@* worker registered in Flower`` — against a demonstrably healthy, correctly
running deployment. The cause has nothing to do with the ``--pool=threads`` GPU workers, and
everything to do with flower 2.1.0's own source:

``flower/app.py``'s ``Flower.start()`` issues exactly ONE ``celery inspect`` broadcast, at
process boot, with a 1-second reply timeout, and caches whatever answered in
``Inspector.workers`` (``flower/inspector.py``). Nothing re-inspects on a timer — an exhaustive
grep of the installed package for ``update_workers``/``PeriodicCallback`` turns up no periodic
inspection call at all. A worker that is still importing torch/whisperx, or running
``@worker_ready preload_models()`` synchronously in its own main thread
(``backend/app/core/celery.py``, ``PRELOAD_GPU_MODELS``), is not ready to answer inside that
one-second window and is absent from the JSON API **forever** — waiting and re-checking cannot
help, because nothing ever asks Flower to look again.

This was independently confirmed live, via ``.claude/worktrees/agent-a25f2b4ae4401c992``'s own
``--fresh fix609`` deployment (a plain ``./opentr.sh start dev``, no ``--gpu-scale``, no GPU
work — five prefork/threads workers lost the exact same race as a GPU worker would): the
unrefreshed ``/api/workers`` returned only 1 of 7 running workers, ``?refresh=1`` (Flower's own
documented parameter, awaited server-side) returned all 7 including every threads-pool one, a
direct ``celery inspect ping`` matched the refreshed set exactly, the HTML ``/workers`` dashboard
(event-derived, a different code path) already showed all 7 even before any refresh, and
Flower's own startup banner logged 33 seconds before the worker's ``ready.`` line.

The fix (root ``CLAUDE.md``'s "never silence a linter" table applies equally to a regression
test: this one exists so the next silently-dropped Flower flag is caught, not muted) has three
parts, each with its own test below:

1. ``docker-compose.yml``'s ``flower:`` command needs ``--inspect_timeout=`` raised well past
   the 1000 ms default, so a request that DOES ask for a refresh survives a GIL-bound worker.
2. Every script that reads Flower's ``/api/workers`` must pass ``?refresh=1`` — the unrefreshed
   endpoint is not a live roster and never will be.
3. ``--queues=...`` and ``--broker=...`` used to sit in the same compose block and did nothing:
   ``flower.command.is_flower_option()`` silently discards any ``--flag`` it does not recognise
   as a real Flower/tornado option, and Flower already resolves the broker from
   ``CELERY_BROKER_URL`` rather than a CLI flag. Deleting them was a no-op fix; the risk is a
   *third* one landing unnoticed, which is what :func:`test_no_flower_flag_is_silently_dropped`
   guards against.

Static tests only — no live stack needed, and no ``backend/app/**`` source changed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"

#: flower/options.py's inspect_timeout default is 1000.0 (ms) — a GIL-bound GPU worker
#: mid preload_models() cannot reliably answer inside that window (issue #609 §1.2/2.2).
MIN_INSPECT_TIMEOUT_MS = 5000

#: A comment line ("# ... /api/workers ...") documents the endpoint; only a line that is
#: not a bare comment is an actual HTTP call site.
_COMMENT_LINE = re.compile(r"^\s*#")
_API_WORKERS = re.compile(r"api/workers\b")
_REFRESH_PARAM = re.compile(r"refresh=1\b")

#: docker-compose.yml is the only file that currently defines a flower: command: block
#: (every overlay only layers image/build/volumes/container_name) — but the check below
#: is derived from every compose file present, not hardcoded to that one, so a future
#: overlay that adds its own flower command is caught too.
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yml"))


def _flower_commands() -> dict[Path, str]:
    """Every compose file with a `flower:` service `command:` string, keyed by path."""
    yaml = pytest.importorskip("yaml", reason="PyYAML parses the compose files")
    found: dict[Path, str] = {}
    for path in COMPOSE_FILES:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        services = document.get("services") or {}
        flower = services.get("flower")
        if not isinstance(flower, dict):
            continue
        command = flower.get("command")
        if isinstance(command, str):
            found[path] = command
    return found


def _flower_command_flags(command: str) -> list[str]:
    """Every `--flag` (or `--flag=value`) token in a flower command string."""
    return [token for token in command.split() if token.startswith("--")]


def _api_workers_call_sites() -> dict[str, str]:
    """Every non-comment line in scripts/**/*.sh that references `api/workers`.

    Keyed ``"<relative path>:<line number>"`` -> the line itself, so a failure names an
    exact location instead of just a filename.
    """
    sites: dict[str, str] = {}
    for path in sorted(SCRIPTS_DIR.rglob("*.sh")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _COMMENT_LINE.match(line):
                continue
            if _API_WORKERS.search(line):
                key = f"{path.relative_to(REPO_ROOT)}:{lineno}"
                sites[key] = line
    return sites


def test_flower_command_sets_an_inspect_timeout() -> None:
    """A widened --inspect_timeout is what lets a ?refresh=1 request survive a GIL-bound
    GPU worker (issue #609 §2.2) — without it, a refresh can still time out at flower's
    1-second default against a worker mid `preload_models()`.
    """
    commands = _flower_commands()
    assert commands, "no compose file defines a flower: service with a command: — did it move?"

    for path, command in commands.items():
        match = re.search(r"--inspect_timeout=(\d+)", command)
        assert match, (
            f"{path.name}'s flower command has no --inspect_timeout= override, so it falls "
            f"back to flower's 1000ms default — too short for a GIL-bound GPU worker to "
            f"answer even an explicit ?refresh=1 broadcast (issue #609)"
        )
        assert int(match.group(1)) >= MIN_INSPECT_TIMEOUT_MS, (
            f"{path.name}'s --inspect_timeout={match.group(1)} is below "
            f"{MIN_INSPECT_TIMEOUT_MS}ms, which is not enough headroom for a worker "
            f"importing torch/whisperx or running preload_models() synchronously"
        )


def test_every_api_workers_caller_forces_a_refresh() -> None:
    """Every script that reads Flower's /api/workers must pass ?refresh=1.

    The unrefreshed endpoint is Flower's cached boot-time snapshot (issue #609) — reading it
    without a refresh will eventually omit whichever worker was still starting up when Flower
    itself booted, permanently, with no way to notice from the response alone.
    """
    sites = _api_workers_call_sites()
    missing = {key: line for key, line in sites.items() if not _REFRESH_PARAM.search(line)}
    assert not missing, (
        "these scripts read Flower's /api/workers without ?refresh=1, so they trust a "
        "boot-time snapshot that can permanently omit a worker still starting up "
        f"(issue #609): {missing}"
    )


def test_the_scan_finds_the_known_callers() -> None:
    """Guard on the guard: a scan matching nothing would pass every assertion above."""
    sites = _api_workers_call_sites()
    assert len(sites) >= 2, (
        f"expected at least 2 api/workers call sites (gpu-scale-smoke.sh and "
        f"bulk-processing-cheatsheet.sh), found {len(sites)} — the scan is not scanning"
    )
    relative_paths = {key.split(":", 1)[0] for key in sites}
    assert "scripts/gpu-scale-smoke.sh" in relative_paths, (
        "the scan no longer finds scripts/gpu-scale-smoke.sh's own /api/workers call — "
        "either the file moved or the regex broke"
    )
    assert "scripts/bulk-processing-cheatsheet.sh" in relative_paths, (
        "the scan no longer finds scripts/bulk-processing-cheatsheet.sh's own "
        "/api/workers call — either the file moved or the regex broke"
    )


def test_no_flower_flag_is_silently_dropped() -> None:
    """Every --flag in the flower command must be a real, recognised Flower option.

    flower.command.is_flower_option() filters argv down to attributes tornado.options
    actually knows about, silently discarding anything else (see
    warn_about_celery_args_used_in_flower_command for the only place a warning is logged,
    which is easy to miss in a boot log). --queues=... and --broker=... both sat in
    docker-compose.yml doing nothing — --queues has never been a real Flower option, and
    --broker is a *celery* option, dropped here and resolved instead from
    CELERY_BROKER_URL. This test is what would have caught either on the day it was added,
    and catches the next one.
    """
    command_mod = pytest.importorskip(
        "flower.command", reason="flower is a direct dependency (requirements.txt/-ci.txt)"
    )

    commands = _flower_commands()
    assert commands, "no compose file defines a flower: service with a command: — did it move?"

    for path, command in commands.items():
        flags = _flower_command_flags(command)
        assert flags, f"{path.name}'s flower command has no --flags at all — did it change shape?"
        dropped = [flag for flag in flags if not command_mod.is_flower_option(flag)]
        assert not dropped, (
            f"{path.name}'s flower command passes flag(s) that "
            f"flower.command.is_flower_option() silently discards — they have never taken "
            f"effect and should be deleted (issue #609 found --queues=... and --broker=... "
            f"in exactly this state): {dropped}"
        )


def test_is_flower_option_can_actually_fail() -> None:
    """Guard on the guard: prove is_flower_option() rejects something, so a version of
    flower that always returns True (or a broken import) can't make the test above pass
    trivially.
    """
    command_mod = pytest.importorskip(
        "flower.command", reason="flower is a direct dependency (requirements.txt/-ci.txt)"
    )
    assert not command_mod.is_flower_option("--queues=gpu,cpu"), (
        "is_flower_option() now accepts --queues — the predicate this test relies on has "
        "changed behaviour upstream, re-derive the assumption"
    )
    assert not command_mod.is_flower_option("--broker=redis://redis:6379/0"), (
        "is_flower_option() now accepts a celery-only --broker flag placed after the "
        "flower subcommand — re-derive the assumption"
    )
    assert command_mod.is_flower_option("--inspect_timeout=10000"), (
        "is_flower_option() now rejects a genuine Flower option — the predicate itself is "
        "broken, not just stricter"
    )
