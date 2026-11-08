"""Refuse every Celery publish on the public demo (issue #628, exit criterion E1).

The owner's acceptance test is "a demo session cannot enqueue any Celery task". The HTTP
guard (``middleware/demo_readonly.py``) refuses writes, but several GET routes enqueue work
(``tests/unit/test_inert_checker.py::GET_ROUTES_THAT_ENQUEUE``: the lazy redaction scan, the
download prepare, the on-demand GPU stats), and a route nobody has found yet would too. So the
enqueue boundary refuses as well.

``Celery.send_task`` is the one funnel: ``Task.apply_async`` (and so ``delay``, chains, groups
and chords) ends in ``app.send_task(...)`` (celery 5.6 ``app/task.py``), and
``before_task_publish`` fires later, inside the AMQP publish. A ``before_task_publish``
receiver cannot refuse: Celery's ``Signal.send`` returns a receiver's exception instead of
raising it. The refusal raises before the signal, so the #898 publish counter stays at zero
and a crawl with ``python -m app.scripts.assert_inert`` measures the demo as inert.

Every call site that enqueues from a request already treats a publish error as "not queued"
(``claim_and_dispatch_redaction_scan`` withdraws its claim, the GPU stats read degrades to
"unavailable"), which is the behaviour the demo wants.

Seeding and resets run with ``DEMO_MODE=false`` (``scripts/demo/demo-reset.sh``), with the
public port closed.
"""

from __future__ import annotations

from typing import Any

from celery import Celery

from app.core.config import settings


class DemoPublishRefusedError(RuntimeError):
    """A task publish attempted while ``DEMO_MODE`` is on."""


class DemoAwareCelery(Celery):
    """``Celery`` whose publishes are refused while ``settings.DEMO_MODE`` is on."""

    def send_task(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Publish ``name``, unless this is the read-only demo."""
        if settings.DEMO_MODE:
            raise DemoPublishRefusedError(f"read-only demo: task {name!r} was not published")
        return super().send_task(name, *args, **kwargs)
