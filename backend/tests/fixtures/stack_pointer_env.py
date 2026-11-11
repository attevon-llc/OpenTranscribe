"""Environment variables that point a shell at one OpenTranscribe stack."""

from __future__ import annotations

#: Variables an operator exports to point a SHELL at one stack (``--fresh`` port offsets,
#: ``source <stack>-env.sh``). A unit test that builds ``Settings(...)`` or runs an
#: ``opentr.sh`` snippet in a child process reads them from the environment too, so the
#: same unit test passed in a clean shell and failed in the P3.2 gate's: an exported
#: ``REDIS_PORT=5577`` turned ``Settings(REDIS_HOST="r")``'s URL into ``r:5577``, and an
#: exported ``COMPOSE_PROJECT_NAME`` widened ``./opentr.sh stop``'s project filter.
#:
#: Deliberately NOT here: ``POSTGRES_*``, ``MINIO_*``, ``OPENSEARCH_*`` and the other
#: ``*_PORT`` variables. Stack-backed tests in this tree read those at test time to reach
#: the stack (``tests/compose_project.py`` resolves the project from ``POSTGRES_PORT``),
#: and the settings singleton has already read everything at import, so clearing a
#: variable here can only change what a NEW ``Settings`` or child process sees.
STACK_POINTER_VARS = (
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PASSWORD",
    "REDIS_URL",
    "CELERY_BROKER_URL",
)
