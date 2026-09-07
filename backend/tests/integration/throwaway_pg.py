"""Shared constants for suites that stand up a throwaway PostgreSQL container.

Not a test module (no ``test_`` prefix), so pytest imports it but collects nothing from it.
"""

from __future__ import annotations

# How long a throwaway postgres gets to accept connections before the fixture gives up.
#
# This was 30 s, copied into five modules, and it is the reason 17 integration tests errored
# with "postgres in ot-*-test-<hash> never became ready" during a full gate run. The budget was
# never the *container's* startup time in isolation — it is startup time while the same docker
# daemon is building overlay images and fielding `docker exec` from 48 pytest workers, and while
# 19 function-scoped fixtures across four modules each create and destroy their own instance.
# Under that contention initdb's two-phase start (see each caller's docstring) routinely runs
# past 30 s.
#
# Raising the ceiling costs nothing on an idle machine: every caller polls to TWO CONSECUTIVE
# successful queries and returns the moment it has them, so a healthy container still clears in
# a second or two. The timeout is only reached on a genuine failure, where a longer wait buys a
# real diagnosis instead of a load-dependent flake.
_READY_TIMEOUT = 180.0
