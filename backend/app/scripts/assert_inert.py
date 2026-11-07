"""Run the assert-inert checker against the real app (issue #898).

Usage (inside the backend container, ``./opentr.sh shell backend``)::

    python -m app.scripts.assert_inert [--json]

Exit codes: 0 inert, 1 not inert, 2 usage error, 3 NOT MEASURED (the publish counter is
absent, so no crawl result can be trusted). What each check does: ``app/core/inert.py`` and ``app/api/inert_routes.py``.
"""

import sys

from app.api.inert_routes import main


def run(argv: list[str] | None = None) -> int:
    """Load the application the way the API process does, then check it."""
    from app.main import app

    return main(argv, app)


if __name__ == "__main__":
    sys.exit(run())
