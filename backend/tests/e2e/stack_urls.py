"""Pure helper for detecting a mixed-stack E2E run (issue #965).

Kept dependency-free (no playwright, no requests, no ``tests.conftest`` import side
effects) so a unit test can exercise the mixed-stack logic without paying for
``conftest.py``'s heavy imports — the same reason ``frontend_warm.py`` and
``timeouts.py`` are their own small modules beside it.
"""

from __future__ import annotations

from urllib.parse import urlparse

#: `opentr.sh`'s `--port-offset` mechanism (`FRESH_PORT_VARS`) moves the frontend and
#: backend ports TOGETHER, by the same offset, on every stack this tooling ever creates:
#: frontend 5173 + N, backend 5174 + N -- the live dev stack is N=0, a
#: `--fresh --port-offset 200` stack is 5373/5374. So the backend port is ALWAYS exactly
#: one more than the frontend port for a single, self-consistent stack. A frontend/backend
#: URL pair that violates this relationship cannot both name the same stack -- which is
#: exactly the silent mixed-stack state issue #965 describes: a browser driven against one
#: stack while the API helpers talk to another.
BACKEND_FRONTEND_PORT_DELTA = 1


def mixed_stack_problem(base_url: str, backend_url: str) -> str | None:
    """Return a diagnostic string when *base_url*/*backend_url* cannot be one stack.

    ``None`` means the pair is consistent with a single stack under this repo's own
    port-offset convention. This is a NECESSARY check, not a sufficient one -- it
    cannot prove two URLs are the same *live* stack (that would need a network call
    this fixture already avoids at collection time), only that they are not related
    the way every stack this tooling creates relates a frontend to its backend. That
    is enough to catch the failure actually observed: `--base-url` pointed at a
    `--fresh` stack while `--backend-url`/`E2E_BACKEND_URL` (or vice versa) still
    named the live one, or the reverse.

    Args:
        base_url: The resolved frontend base URL (what the browser will drive).
        backend_url: The resolved backend base URL (what the API helpers will call).

    Returns:
        ``None`` if consistent, else a human-readable explanation of the mismatch.
    """
    base = urlparse(base_url)
    backend = urlparse(backend_url)

    if base.hostname != backend.hostname:
        return (
            "E2E frontend/backend URLs point at DIFFERENT HOSTS -- this cannot be one "
            f"stack: base_url={base_url!r} (host {base.hostname!r}), "
            f"backend_url={backend_url!r} (host {backend.hostname!r}). A browser on one "
            "stack and API helpers on another is a silent mixed-stack run (issue #965) -- "
            "pass matching --base-url/--backend-url (or E2E_FRONTEND_URL/E2E_BACKEND_URL) "
            "from the SAME deployment."
        )

    base_port = base.port or (443 if base.scheme == "https" else 80)
    backend_port = backend.port or (443 if backend.scheme == "https" else 80)

    if backend_port - base_port != BACKEND_FRONTEND_PORT_DELTA:
        return (
            "E2E frontend/backend URLs do not belong to the same stack: every stack this "
            "tooling creates publishes its backend on frontend_port + "
            f"{BACKEND_FRONTEND_PORT_DELTA} (opentr.sh's --port-offset moves both together — "
            "see FRESH_PORT_VARS), but got base_url="
            f"{base_url!r} (port {base_port}) and backend_url={backend_url!r} "
            f"(port {backend_port}). A browser on one stack and API helpers on another is a "
            "silent mixed-stack run (issue #965) -- pass matching --base-url/--backend-url "
            "(or E2E_FRONTEND_URL/E2E_BACKEND_URL), e.g. both from the same "
            "--fresh --port-offset deployment."
        )

    return None
