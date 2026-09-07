"""CUDA device guard (issue #719).

The fast suite used to open a CUDA context on every visible GPU from any xdist
worker that happened to import torch, including cards this project does not
own. `tests/conftest.py`'s `pytest_configure`/`pytest_runtest_setup` now hide
every device from the default selection and hard-fail a `gpu`-marked test that
somehow ran under the guard instead of silently skipping it.

This module intentionally carries NO `gpu` marker — it is exercised by the
default fast-suite selection, which is the state the guard must produce.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.conftest import _selection_may_run_gpu_tests

_GUARD_SENTINEL = "-1"


def test_the_default_selection_has_no_visible_cuda_device() -> None:
    """7.1: in-process, runs inside an xdist worker under the default -n auto."""
    hatch = os.environ.get("OT_TEST_CUDA_VISIBLE_DEVICES")
    if hatch is not None and hatch != "all":
        assert os.environ.get("CUDA_VISIBLE_DEVICES") == hatch
    elif hatch == "all":
        # Guard deliberately not applied: CUDA_VISIBLE_DEVICES is whatever the
        # parent shell handed us. Assert the ONLY thing that is actually
        # guaranteed — that the guard left it exactly as `OT_TEST_EXPECT_CVD`
        # (set by the "all" case of test_the_escape_hatch_selects_the_requested_
        # devices below) rather than overwriting it with the sentinel. torch is
        # deliberately not imported in this branch: the ambient value may be a
        # real device, and importing torch would open a context on it.
        expected = os.environ.get("OT_TEST_EXPECT_CVD")
        assert expected is not None, "expects OT_TEST_EXPECT_CVD to be set by the caller"
        assert os.environ.get("CUDA_VISIBLE_DEVICES") == expected
        return
    else:
        assert os.environ.get("CUDA_VISIBLE_DEVICES") == _GUARD_SENTINEL

    import torch

    assert torch.cuda.device_count() == 0
    assert torch.cuda.is_available() is False


@pytest.mark.gpu
def test_gpu_probe_sees_released_device_visibility() -> None:
    """7.2: deselected by default; driven explicitly by test 7.3 in a subprocess."""
    assert os.environ.get("CUDA_VISIBLE_DEVICES") != _GUARD_SENTINEL


def test_a_gpu_selection_is_not_blinded() -> None:
    """7.3: `-m gpu` must not be blinded by the guard (anti-vacuum control)."""
    env = dict(os.environ)
    env.pop("CUDA_VISIBLE_DEVICES", None)
    env.pop("OT_TEST_CUDA_VISIBLE_DEVICES", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            __file__ + "::test_gpu_probe_sees_released_device_visibility",
            "-o",
            "addopts=",
            "-m",
            "gpu",
            "-q",
            "-n0",
            "-p",
            "no:cacheprovider",
        ],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0
    assert "1 passed" in result.stdout


def test_a_gpu_marked_test_cannot_run_blinded() -> None:
    """7.4: an empty markexpr selects `gpu` tests too — must ERROR, never skip."""
    env = dict(os.environ)
    env.pop("CUDA_VISIBLE_DEVICES", None)
    env.pop("OT_TEST_CUDA_VISIBLE_DEVICES", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            __file__ + "::test_gpu_probe_sees_released_device_visibility",
            "-o",
            "addopts=",
            "-q",
            "-n0",
        ],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1
    assert "cuda-device-guard" in result.stdout


@pytest.mark.parametrize(
    ("markexpr", "expected"),
    [
        ("not integration and not gpu", False),
        ("gpu", True),
        ("integration", True),
        ("not integration", True),
        ("not gpu", False),
        ("slow", True),
        ("gpu and not slow", True),
        ("e2e or gpu", True),
        ("", False),
    ],
)
def test_the_marker_predicate_rejects_the_default_expression(markexpr: str, expected: bool) -> None:
    """7.5: table-driven, prototyped against real `_pytest.mark.expression`."""
    assert _selection_may_run_gpu_tests(markexpr) is expected


def test_the_marker_predicate_fails_safe_without_the_private_api(monkeypatch) -> None:
    """7.5 fallback case: no silent blind if `_pytest.mark.expression` moves."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "_pytest.mark.expression":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    assert _selection_may_run_gpu_tests("not integration and not gpu") is True


@pytest.mark.parametrize("hatch_value", ["-1", "all"])
def test_the_escape_hatch_selects_the_requested_devices(hatch_value: str) -> None:
    """7.6: only `-1`/`all` are used, so this never exposes a real card."""
    env = dict(os.environ)
    env["OT_TEST_CUDA_VISIBLE_DEVICES"] = hatch_value
    env["CUDA_VISIBLE_DEVICES"] = "0"
    ambient = env["CUDA_VISIBLE_DEVICES"]
    # Read by the "all" branch of test_the_default_selection_has_no_visible_cuda_device
    # so it can assert the ambient value survived rather than merely returning early.
    env["OT_TEST_EXPECT_CVD"] = ambient
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            __file__ + "::test_the_default_selection_has_no_visible_cuda_device",
            "-o",
            "addopts=",
            "-m",
            "not integration and not gpu",
            "-q",
            "-n0",
            "-p",
            "no:cacheprovider",
        ],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0
    assert "1 passed" in result.stdout
    if hatch_value != "all":
        assert ambient == "0"  # documents the ambient value the hatch overrode


def test_an_ambient_cuda_visible_devices_does_not_release_the_guard() -> None:
    """7.7: the `setdefault` trap — an ambient value must not survive."""
    env = dict(os.environ)
    env.pop("OT_TEST_CUDA_VISIBLE_DEVICES", None)
    env["CUDA_VISIBLE_DEVICES"] = "0"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            __file__ + "::test_the_default_selection_has_no_visible_cuda_device",
            "-o",
            "addopts=",
            "-m",
            "not integration and not gpu",
            "-q",
            "-n0",
            "-p",
            "no:cacheprovider",
        ],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0
    assert "1 passed" in result.stdout
