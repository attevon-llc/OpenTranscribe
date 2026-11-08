"""What this deployment's image can do, and what the deployment permits (issue #628).

Split out of ``app.core.config`` (at its size cap) as one cohesive concern. ``Settings``
inherits these fields; the env source, ``env_ignore_empty`` and ``extra="ignore"`` come from
``Settings``' own ``model_config``, and this class is never instantiated on its own.

Two orthogonal axes, never merged into one variable:

* ``DEPLOYMENT_MODE`` is what the IMAGE can do. Readers compare it against exact strings
  (``services/asr/factory.py`` refuses local ASR for ``"lite"``), so an unknown value would
  fall into every reader's ``else`` branch and re-arm local ASR on an image that has none.
  The vocabulary is therefore closed and checked at startup.
* ``DEMO_MODE`` is what the DEPLOYMENT permits: a public, read-only demo refuses every write
  (``middleware/demo_readonly.py``) and every task publish (``core/celery.py``). A demo keeps
  ``DEPLOYMENT_MODE=lite``.
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings

VALID_DEPLOYMENT_MODES = frozenset({"full", "lite"})


class DeploymentSettings(BaseSettings):
    DEPLOYMENT_MODE: str = "full"

    # Public read-only demo (#628). Off on every self-hosted deployment.
    DEMO_MODE: bool = False
    # Per-client-IP ceiling on every route, applied only when DEMO_MODE is on: a public demo
    # is reached by strangers, and the per-route auth limits do not cover the read surface.
    DEMO_RATE_LIMIT_PER_MINUTE: int = 120
    # The one shared, ordinary (non-admin) account visitors sign in as; created by the seeder
    # (``python -m app.scripts.demo_seed import``). Unused unless DEMO_MODE is on.
    DEMO_USER_EMAIL: str = ""
    DEMO_USER_PASSWORD: str = ""

    @field_validator("DEPLOYMENT_MODE", mode="after")
    @classmethod
    def _closed_deployment_mode(cls, v: str) -> str:
        """Normalise to one of ``VALID_DEPLOYMENT_MODES``; refuse anything else at startup."""
        mode = v.strip().lower()
        if mode not in VALID_DEPLOYMENT_MODES:
            raise ValueError(
                f"DEPLOYMENT_MODE must be one of {sorted(VALID_DEPLOYMENT_MODES)}, got {v!r}"
            )
        return mode
