"""Run a script under test with SIGINT as a terminal would deliver it.

POSIX lets a non-interactive shell start background commands with SIGINT ignored, and that
disposition is inherited across fork and exec. A pytest launched with ``&`` from a script (a
backgrounded gate sweep) therefore hands SIG_IGN to every subprocess it starts, and a
non-interactive bash cannot trap a signal that was ignored on entry: ``trap '...' INT`` is
silently a no-op, the SIGINT a test sends is dropped, and the script exits 0. That is how the
SIGINT legs of the interrupt tests failed in a full sweep while passing from a terminal.

:func:`with_default_sigint` resets SIGINT to SIG_DFL and execs the real command (same pid), so
the script starts the way Ctrl-C in a terminal finds it. :func:`assert_signal_is_caught` reads
the kernel's own record of the process's handlers, so a test signals only once the trap is armed
and otherwise fails with the reason rather than with a wrong exit code.
"""

from __future__ import annotations

import sys
from pathlib import Path

_RESET_AND_EXEC = (
    "import os, signal, sys; "
    "signal.signal(signal.SIGINT, signal.SIG_DFL); "
    "os.execvp(sys.argv[1], sys.argv[1:])"
)


def with_default_sigint(argv: list[str]) -> list[str]:
    """``argv`` prefixed so it is exec'd with SIGINT at SIG_DFL, whatever this process inherited."""
    return [sys.executable, "-c", _RESET_AND_EXEC, *argv]


def _mask(status: str, field: str) -> int:
    for line in status.splitlines():
        if line.startswith(f"{field}:"):
            return int(line.split()[1], 16)
    raise AssertionError(f"no {field} line in /proc status:\n{status}")


def assert_signal_is_caught(pid: int, sig: int) -> None:
    """Fail unless process ``pid`` has a handler installed for ``sig`` (a bash trap is one)."""
    status = Path(f"/proc/{pid}/status").read_text()
    bit = 1 << (sig - 1)
    assert not _mask(status, "SigIgn") & bit, (
        f"pid {pid} ignores signal {sig}: it was ignored when the shell started, so its trap "
        "cannot be armed and the signal would be dropped"
    )
    assert _mask(status, "SigCgt") & bit, f"pid {pid} has no handler for signal {sig} yet"
