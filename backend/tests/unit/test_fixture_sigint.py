"""Guard the guard: ``fixtures.sigint`` must notice an ignored SIGINT, and must undo it.

The interrupt tests of ``run-backend-tests.sh`` and ``run-pki-e2e-leg.sh`` depend on both
halves. Without the first, a sweep launched in the background reports "0 == 130" with no
reason; without the second, those tests cannot pass there at all.
"""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest
from fixtures.sigint import assert_signal_is_caught
from fixtures.sigint import with_default_sigint

_TRAPPED = ["bash", "-c", "trap 'exit 130' INT; echo ready; read -r _; exit 0"]
_IGNORE_THEN_EXEC = (
    "import os, signal, sys; "
    "signal.signal(signal.SIGINT, signal.SIG_IGN); "
    "os.execvp(sys.argv[1], sys.argv[1:])"
)


def _start(argv: list[str]) -> subprocess.Popen[str]:
    proc = subprocess.Popen(  # fixed argv; bash from PATH
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _finish(proc: subprocess.Popen[str]) -> int:
    proc.communicate(timeout=30)
    return proc.returncode


def test_an_inherited_ignore_is_reported_not_signalled_into():
    proc = _start([sys.executable, "-c", _IGNORE_THEN_EXEC, *_TRAPPED])
    try:
        with pytest.raises(AssertionError, match="ignores signal 2"):
            assert_signal_is_caught(proc.pid, signal.SIGINT)
        proc.send_signal(signal.SIGINT)
    finally:
        rc = _finish(proc)
    assert rc == 0, "the trap ran even though SIGINT was ignored on entry: the premise is wrong"


def test_with_default_sigint_arms_the_trap_even_under_an_inherited_ignore():
    argv = [sys.executable, "-c", _IGNORE_THEN_EXEC, *with_default_sigint(_TRAPPED)]
    proc = _start(argv)
    try:
        assert_signal_is_caught(proc.pid, signal.SIGINT)
        proc.send_signal(signal.SIGINT)
    finally:
        rc = _finish(proc)
    assert rc == 130
