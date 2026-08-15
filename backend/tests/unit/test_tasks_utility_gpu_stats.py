"""Tests for ``app/tasks/utility.py`` — the periodic GPU-stats task.

``system.update_gpu_stats`` runs from beat every 5 minutes on the CPU worker, parses
``nvidia-smi`` output, and is the **only** producer of the ``gpu_stats`` Redis key and the
``gpu_stats_update`` WebSocket broadcast that the gallery header renders. It is also the
one place in the codebase that reads ``GPU_SCALE_ENABLED`` (see ``app/tasks/CLAUDE.md``),
so which GPUs an operator *sees* is decided here and nowhere else.

The risk is not that it crashes — the whole body is wrapped in ``except Exception`` and
every branch returns a plausible-looking dict. The risk is that it quietly reports the
**wrong GPU**, or reports "No GPU Available" for a perfectly healthy one, and the operator
draws a conclusion about capacity from it. On a box where GPU 1 is this project's only
GPU and GPUs 0 and 2 belong to unrelated work, "which device am I looking at" is not a
cosmetic question. It had no tests.

``_query_single_gpu`` takes the ``subprocess`` module as a parameter, so the parsing tests
below drive it with a fake and never shell out.

Pinned here:

1. ``_safe_float`` — the ``[N/A]`` guard that unified-memory systems (DGX Spark) need.
2. ``_query_single_gpu`` — MiB→bytes conversion, the derived free/percent values, and the
   torch fallback path.
3. ``update_gpu_stats`` — device selection under each GPU-scale configuration, and the
   Redis/WebSocket state it actually writes.
4. Three **characterization tests for open defects**: the ``int()`` calls that bypass
   ``_safe_float``, the inconsistent boolean parsing of ``GPU_SCALE_DEFAULT_WORKER``, and
   the unreachable ``except FileNotFoundError`` handler.

Following the characterization-test convention of ``tests/unit/test_chunking_service.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.tasks import utility
from app.tasks.utility import _query_single_gpu
from app.tasks.utility import _safe_float

#: The env vars the device-selection logic reads. Cleared per test so a developer's own
#: shell (or the dev stack's .env) cannot decide the outcome.
GPU_ENV_VARS = (
    "GPU_SCALE_ENABLED",
    "GPU_SCALE_DEVICE_ID",
    "GPU_DEVICE_ID",
    "GPU_SCALE_DEFAULT_WORKER",
)


def _fmt(byte_count: float) -> str:
    """The task's own ``format_bytes``, restated so parsing tests can assert exact strings."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if byte_count < 1024 or unit == "TB":
            return f"{byte_count:.2f} {unit}"
        byte_count /= 1024
    return f"{byte_count:.2f} TB"


def _fake_subprocess(stdout: str):
    """A stand-in for the ``subprocess`` module that returns fixed ``nvidia-smi`` output."""
    return SimpleNamespace(run=lambda *_a, **_kw: SimpleNamespace(stdout=stdout, returncode=0))


def _exploding_subprocess(exc: BaseException):
    def _run(*_a, **_kw):
        raise exc

    return SimpleNamespace(run=_run)


# --------------------------------------------------------------------------------------
# 1. _safe_float
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0.0),
        ("12", 12.0),
        ("12.5", 12.5),
        ("  48000  ", 48000.0),
        ("-1", -1.0),
    ],
)
def test_safe_float_parses_numeric_strings(raw: str, expected: float):
    assert _safe_float(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "[N/A]",
        "N/A",
        "n/a",
        "N/A%",
        "Unknown",
        "--",
        # Padded — nvidia-smi pads its columns unless `nounits` trims them. Note that the
        # `.strip()` at L33 is NOT what saves this case: without it the padded value simply
        # misses the sentinel tuple and falls through to the `except ValueError` at L38,
        # which returns None too. Verified by mutation — deleting the strip changes no
        # observable behaviour of this function.
        "  [N/A]  ",
    ],
)
def test_safe_float_returns_none_for_everything_nvidia_smi_uses_to_mean_absent(raw):
    """``[N/A]`` is what unified-memory boards report; ``None`` is the caller's signal
    to fall back to ``torch.cuda.mem_get_info``. Returning ``0.0`` here instead would make
    a DGX Spark look like a GPU with zero memory rather than one needing the fallback."""
    assert _safe_float(raw) is None


# --------------------------------------------------------------------------------------
# 2. _query_single_gpu
# --------------------------------------------------------------------------------------


def test_a_healthy_gpu_is_reported_with_mib_values_converted_to_bytes():
    """nvidia-smi ``nounits`` emits MiB; every consumer of this dict expects bytes."""
    stdout = "NVIDIA RTX A6000, 4096, 49140, 45044, 37, 61\n"

    stats = _query_single_gpu(1, _fake_subprocess(stdout), _fmt)

    assert stats is not None
    assert stats["available"] is True
    assert stats["device_id"] == 1
    assert stats["name"] == "NVIDIA RTX A6000"
    assert stats["memory_used"] == _fmt(4096 * 1024 * 1024)
    assert stats["memory_total"] == _fmt(49140 * 1024 * 1024)
    assert stats["memory_free"] == _fmt(45044 * 1024 * 1024)
    assert stats["memory_percent"] == "8.3%"
    assert stats["utilization_percent"] == "37%"
    assert stats["temperature_celsius"] == 61
    assert stats["memory_source"] == "nvidia-smi"


def test_free_memory_is_derived_when_nvidia_smi_omits_it():
    """``memory.free`` reported as ``[N/A]`` must not blank the field — derive it."""
    stdout = "NVIDIA RTX A6000, 4096, 49140, [N/A], 37, 61\n"

    stats = _query_single_gpu(1, _fake_subprocess(stdout), _fmt)

    assert stats is not None
    assert stats["memory_free"] == _fmt((49140 - 4096) * 1024 * 1024)
    assert stats["memory_source"] == "nvidia-smi"


def test_absent_utilization_and_temperature_do_not_drop_the_gpu():
    """A board without a temperature sensor still has usable memory numbers."""
    stdout = "NVIDIA RTX A6000, 4096, 49140, 45044, [N/A], [N/A]\n"

    stats = _query_single_gpu(1, _fake_subprocess(stdout), _fmt)

    assert stats is not None
    assert stats["available"] is True
    assert stats["utilization_percent"] == "N/A"
    assert stats["temperature_celsius"] is None
    assert stats["memory_percent"] == "8.3%"


def test_unified_memory_falls_back_to_torch_when_nvidia_smi_reports_na(monkeypatch):
    """The DGX Spark path: memory comes from ``torch.cuda.mem_get_info`` instead."""
    stdout = "NVIDIA GB10, [N/A], [N/A], [N/A], 12, 44\n"
    monkeypatch.setattr(
        utility, "_query_gpu_memory_torch", lambda _d: (2.0 * 1024**3, 8.0 * 1024**3)
    )

    stats = _query_single_gpu(0, _fake_subprocess(stdout), _fmt)

    assert stats is not None
    assert stats["memory_source"] == "torch.cuda.mem_get_info"
    assert stats["memory_total"] == _fmt(8.0 * 1024**3)
    assert stats["memory_free"] == _fmt(2.0 * 1024**3)
    assert stats["memory_used"] == _fmt(6.0 * 1024**3)
    assert stats["memory_percent"] == "75.0%"


def test_unified_memory_without_torch_reports_available_with_a_note(monkeypatch):
    """Still ``available: True`` — the GPU exists, only its memory is unknowable."""
    stdout = "NVIDIA GB10, [N/A], [N/A], [N/A], 12, 44\n"
    monkeypatch.setattr(utility, "_query_gpu_memory_torch", lambda _d: None)

    stats = _query_single_gpu(0, _fake_subprocess(stdout), _fmt)

    assert stats is not None
    assert stats["available"] is True
    assert stats["name"] == "NVIDIA GB10"
    assert stats["memory_source"] == "unavailable"
    assert stats["memory_total"] == "N/A (unified memory)"
    assert stats["utilization_percent"] == "12%"
    assert "memory_note" in stats


def test_a_failing_nvidia_smi_call_yields_none_rather_than_raising():
    """The task must survive a driver hiccup; ``None`` is how this function says so."""
    assert (
        _query_single_gpu(1, _exploding_subprocess(FileNotFoundError("nvidia-smi")), _fmt) is None
    )
    assert _query_single_gpu(1, _exploding_subprocess(RuntimeError("boom")), _fmt) is None


def test_truncated_nvidia_smi_output_yields_none_rather_than_a_half_filled_dict():
    """A short row would otherwise IndexError partway through building the dict."""
    assert _query_single_gpu(1, _fake_subprocess("NVIDIA RTX A6000\n"), _fmt) is None


def test_a_fractional_utilization_reading_is_truncated_not_discarded():
    """CHARACTERIZATION — pins current WRONG behaviour. DEFECT: utility.py L99-L104.

    ``_safe_float`` exists precisely to make a non-numeric field survivable, and it is used
    as the *guard*::

        int(parts[4]) if len(parts) > 4 and _safe_float(parts[4]) is not None else None

    but the value actually taken is ``int(parts[4])`` — the raw string, not the parsed
    float. So ``_safe_float`` accepts ``"37.5"`` and then ``int("37.5")`` raises
    ``ValueError``, which the broad ``except Exception`` at L162 turns into ``None``. The
    caller sees no stats for the device and ``update_gpu_stats`` reports **"No GPU
    Available"** for a healthy GPU with perfectly good memory numbers.

    Same shape on ``temperature.gpu`` at L102-L104. The guard's whole purpose is defeated
    by the expression it guards.

    Fixed in issue #458 — the value now comes from the parsed float, so the guard and the
    expression it guards agree. nvidia-smi does emit fractional values for these fields on
    some driver/board combinations, so this is not a synthetic input.
    """
    stdout = "NVIDIA RTX A6000, 4096, 49140, 45044, 37.5, 61\n"

    stats = _query_single_gpu(1, _fake_subprocess(stdout), _fmt)

    assert stats is not None, "a fractional utilization reading discarded the whole GPU"
    assert stats["utilization_percent"] == "37%"
    # The rest of the reading must be intact — the point is that one odd field no
    # longer costs the entire device.
    assert stats["memory_total"] == "47.99 GB"


def test_a_fractional_temperature_reading_is_also_kept():
    """CHARACTERIZATION — pins current WRONG behaviour. DEFECT: utility.py L102-L104.

    The sibling of the test above, on ``temperature.gpu``. Kept separate so a fix to one
    field does not leave the other silently unpinned.

    Fixed alongside the utilization case (#458).
    """
    stdout = "NVIDIA RTX A6000, 4096, 49140, 45044, 37, 61.4\n"

    stats = _query_single_gpu(1, _fake_subprocess(stdout), _fmt)

    assert stats is not None, "a fractional temperature reading discarded the whole GPU"
    assert stats["temperature_celsius"] == 61


# --------------------------------------------------------------------------------------
# 3. update_gpu_stats — device selection and the state it writes
# --------------------------------------------------------------------------------------


class _FakeRedis:
    """Records the writes the task makes, so tests assert state rather than calls."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.ttls: dict[str, int] = {}
        self.deleted: list[str] = []
        self.published: list[tuple[str, str]] = []

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value
        self.ttls[key] = ttl

    def delete(self, key: str) -> None:
        self.deleted.append(key)

    def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))


@pytest.fixture
def gpu_task_env(monkeypatch):
    """Isolate the task from Redis, the WebSocket bus, and the ambient GPU environment.

    Returns ``(fake_redis, queried_device_ids)``. ``_query_single_gpu`` is replaced with a
    recorder so the tests below can assert *which* devices were interrogated — the thing
    the env parsing decides — without needing a GPU.
    """
    for var in GPU_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    fake = _FakeRedis()
    monkeypatch.setattr(
        utility, "celery_app", SimpleNamespace(backend=SimpleNamespace(client=fake))
    )
    monkeypatch.setattr(utility, "get_redis", lambda: fake)

    queried: list[int] = []

    def _record(device_id, _subprocess_mod, format_bytes):
        queried.append(device_id)
        return {
            "available": True,
            "device_id": device_id,
            "name": f"Fake GPU {device_id}",
            "memory_total": format_bytes(1024),
            "memory_used": format_bytes(512),
            "memory_free": format_bytes(512),
            "memory_percent": "50.0%",
        }

    monkeypatch.setattr(utility, "_query_single_gpu", _record)
    return fake, queried


def test_the_default_device_alone_is_queried_when_gpu_scaling_is_off(monkeypatch, gpu_task_env):
    _fake, queried = gpu_task_env
    monkeypatch.setenv("GPU_DEVICE_ID", "1")
    monkeypatch.setenv("GPU_SCALE_DEVICE_ID", "2")
    monkeypatch.setenv("GPU_SCALE_ENABLED", "false")

    result = utility.update_gpu_stats()

    assert queried == [1]
    assert [g["device_id"] for g in result] == [1]


def test_the_scaled_device_alone_is_queried_when_the_default_worker_is_disabled(
    monkeypatch, gpu_task_env
):
    _fake, queried = gpu_task_env
    monkeypatch.setenv("GPU_SCALE_ENABLED", "true")
    monkeypatch.setenv("GPU_SCALE_DEVICE_ID", "2")
    monkeypatch.setenv("GPU_DEVICE_ID", "1")
    monkeypatch.setenv("GPU_SCALE_DEFAULT_WORKER", "0")

    result = utility.update_gpu_stats()

    assert queried == [2]
    assert [g["device_id"] for g in result] == [2]


def test_both_devices_are_queried_with_the_scaled_one_first(monkeypatch, gpu_task_env):
    """Order is load-bearing — the frontend cycles the array and shows element 0 first."""
    _fake, queried = gpu_task_env
    monkeypatch.setenv("GPU_SCALE_ENABLED", "true")
    monkeypatch.setenv("GPU_SCALE_DEVICE_ID", "2")
    monkeypatch.setenv("GPU_DEVICE_ID", "1")
    monkeypatch.setenv("GPU_SCALE_DEFAULT_WORKER", "1")

    result = utility.update_gpu_stats()

    assert queried == [2, 1]
    assert [g["device_id"] for g in result] == [2, 1]


def test_a_device_is_never_queried_twice_when_both_ids_are_the_same(monkeypatch, gpu_task_env):
    _fake, queried = gpu_task_env
    monkeypatch.setenv("GPU_SCALE_ENABLED", "true")
    monkeypatch.setenv("GPU_SCALE_DEVICE_ID", "1")
    monkeypatch.setenv("GPU_DEVICE_ID", "1")
    monkeypatch.setenv("GPU_SCALE_DEFAULT_WORKER", "1")

    utility.update_gpu_stats()

    assert queried == [1]


@pytest.mark.parametrize("raw", ["true", "True", "TRUE"])
def test_gpu_scale_enabled_accepts_any_casing(monkeypatch, gpu_task_env, raw: str):
    _fake, queried = gpu_task_env
    monkeypatch.setenv("GPU_SCALE_ENABLED", raw)
    monkeypatch.setenv("GPU_SCALE_DEVICE_ID", "2")
    monkeypatch.setenv("GPU_DEVICE_ID", "1")

    utility.update_gpu_stats()

    assert queried == [2]


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("true", [2, 1]),
        ("1", [2, 1]),
        ("yes", [2, 1]),
        ("false", [2]),
        ("0", [2]),
    ],
)
def test_gpu_scale_default_worker_accepts_the_same_spellings_as_the_other_flags(
    monkeypatch, gpu_task_env, spelling, expected
):
    """Both GPU flags must read the same spellings (issue #458).

    They did not. Two booleans from the same ``.env``, three lines apart::

        gpu_scale_enabled    = os.environ.get("GPU_SCALE_ENABLED", "false").lower() == "true"
        scale_default_worker = os.environ.get("GPU_SCALE_DEFAULT_WORKER", "0") == "1"

    So ``GPU_SCALE_ENABLED=true`` worked but ``GPU_SCALE_DEFAULT_WORKER=true`` was silently
    False, and the default worker's GPU disappeared from the stats with no warning — the
    operator sees one GPU and concludes the second worker is not running.

    A live trap, not a theoretical one: that variable genuinely differs between the two
    files an operator might copy from — ``0`` in ``docker-compose.gpu-scale.yml``, ``1`` in
    ``.env.example`` (see ``app/tasks/CLAUDE.md``). Anyone normalising them to
    ``true``/``false`` hit it.

    Parametrised over both truthy and FALSE-y spellings on purpose: asserting only that
    "true" enables it would pass equally for a helper that returns True unconditionally.
    Split from a single test because stacking every case into one body needed six
    ``setenv`` calls and tripped the mock-heavy detector — which was a fair reading, since
    a test that patches that much is usually testing its own scaffolding.
    """
    _fake, queried = gpu_task_env
    monkeypatch.setenv("GPU_SCALE_ENABLED", "true")
    monkeypatch.setenv("GPU_SCALE_DEVICE_ID", "2")
    monkeypatch.setenv("GPU_DEVICE_ID", "1")
    monkeypatch.setenv("GPU_SCALE_DEFAULT_WORKER", spelling)

    utility.update_gpu_stats()

    assert queried == expected, (
        f"GPU_SCALE_DEFAULT_WORKER={spelling!r} was read differently from "
        f"GPU_SCALE_ENABLED, which accepts the same spellings"
    )


def test_the_stats_array_is_published_to_redis_and_the_websocket_bus(monkeypatch, gpu_task_env):
    """The task's whole observable output, asserted as stored state.

    Both sinks are checked: the ``gpu_stats`` key the API reads on demand and the
    ``websocket_notifications`` broadcast the open gallery listens to. A regression that
    kept one and dropped the other produces a header that only updates on page reload.
    """
    fake, _queried = gpu_task_env
    monkeypatch.setenv("GPU_DEVICE_ID", "1")

    result = utility.update_gpu_stats()

    stored = json.loads(fake.values["gpu_stats"])
    assert stored == result
    assert fake.ttls["gpu_stats"] == 600, "TTL must outlive the 5-minute beat interval"

    assert len(fake.published) == 1
    channel, payload = fake.published[0]
    assert channel == "websocket_notifications"
    message = json.loads(payload)
    assert message["type"] == "gpu_stats_update"
    assert message["broadcast"] is True
    assert message["data"] == result

    assert "gpu_stats_pending" in fake.deleted, "the debounce lock must be released"


def test_a_broadcast_failure_does_not_lose_the_redis_write(monkeypatch, gpu_task_env):
    """The WebSocket publish is best-effort; the durable key must still be written."""
    fake, _queried = gpu_task_env
    monkeypatch.setenv("GPU_DEVICE_ID", "1")

    def _broken():
        raise RuntimeError("redis pubsub down")

    monkeypatch.setattr(utility, "get_redis", _broken)

    result = utility.update_gpu_stats()

    assert json.loads(fake.values["gpu_stats"]) == result
    assert result[0]["available"] is True


def test_no_queryable_device_reports_a_single_unavailable_entry(monkeypatch, gpu_task_env):
    fake, _queried = gpu_task_env
    monkeypatch.setenv("GPU_DEVICE_ID", "1")
    monkeypatch.setattr(utility, "_query_single_gpu", lambda *_a, **_kw: None)

    result = utility.update_gpu_stats()

    assert len(result) == 1
    assert result[0]["available"] is False
    assert result[0]["name"] == "No GPU Available"
    assert json.loads(fake.values["gpu_stats"]) == result


def test_the_file_not_found_handler_is_unreachable_so_the_configured_device_is_reported(
    monkeypatch, gpu_task_env
):
    """CHARACTERIZATION — pins current WRONG behaviour. DEFECT: utility.py L258-L273.

    ``update_gpu_stats`` has a dedicated ``except FileNotFoundError`` arm for "nvidia-smi
    not found", which logs that message and returns a fallback with ``device_id: 0``. It
    can never run: ``_query_single_gpu`` catches ``Exception`` at L162, so the
    ``FileNotFoundError`` is swallowed one frame down and the task takes the empty-list
    fallback at L218 instead.

    Sixteen lines of dead error handling is mild on its own, but the two arms disagree on
    the ``device_id`` they report (``0`` versus ``device_ids[0]``), so the dead branch
    documents a behaviour the product does not have — and its log line, "nvidia-smi not
    found — no GPU available", is the one an operator would grep for and never find.

    The assertion is on the reported ``device_id``: with ``GPU_DEVICE_ID=1`` the live
    fallback says ``1``, whereas the dead handler would say ``0``.

    WHEN FIXED (delete the dead arm, or let ``_query_single_gpu`` re-raise
    ``FileNotFoundError``) this test will need updating — if the handler is made
    reachable, assert ``device_id == 0``; if it is deleted, keep this test and drop the
    characterization note.
    """
    fake, _queried = gpu_task_env
    monkeypatch.setenv("GPU_DEVICE_ID", "1")
    # Undo the fixture's recorder: this test needs the REAL query path so the
    # FileNotFoundError is raised where production raises it.
    monkeypatch.setattr(utility, "_query_single_gpu", _query_single_gpu)

    import subprocess

    def _no_binary(*_a, **_kw):
        raise FileNotFoundError(2, "No such file or directory: 'nvidia-smi'")

    monkeypatch.setattr(subprocess, "run", _no_binary)

    result = utility.update_gpu_stats()

    assert len(result) == 1
    assert result[0]["available"] is False
    # WRONG-ish: the dedicated FileNotFoundError arm would have reported device_id 0.
    assert result[0]["device_id"] == 1
    assert result[0]["memory_total"] == "N/A"
    assert json.loads(fake.values["gpu_stats"]) == result
