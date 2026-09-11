"""The two third-party contracts #809's requeue silently depends on.

``requeue_after_abort`` raises ``Reject(requeue=True)`` and stops there. Everything that makes
that the RIGHT thing to raise happens inside celery and kombu:

1. **celery** must not treat a Reject as a task failure — if it did, the chain's ``link_error``
   would fire and ``on_pipeline_error`` would mark the file ERROR, so every graceful restart
   would look to the user exactly like the crash it replaced.
2. **kombu's Redis transport** must actually put the rejected message back on the queue. Redis
   is not AMQP; it emulates acks, and "requeue" is a concrete ``LPUSH`` in that emulation, not
   a protocol guarantee.

Neither is covered by our own code, so neither would be noticed breaking. A celery or kombu
bump that changed either one would silently convert every graceful shutdown into a
user-visible failure (1) or a lost transcription (2) — with a fully green suite. These tests
are the tripwire.

They deliberately drive the REAL libraries: a throwaway in-memory celery app for (1), the
installed ``kombu.transport.redis`` classes for (2). Mocking either would only prove the mock.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from celery import Celery
from celery.app.trace import build_tracer
from celery.exceptions import Reject

#: A throwaway app, never the real ``celery_app``: this asserts a property of CELERY, and the
#: real app's Redis result backend would need a live broker to store the failure through.
_guard_app = Celery("abort_requeue_guard", broker="memory://", backend="cache+memory://")


@_guard_app.task(name="guard.rejects_with_requeue")
def _rejects_with_requeue():
    """Stands in for a transcription task whose abort branch called requeue_after_abort."""
    raise Reject(requeue=True)


@_guard_app.task(name="guard.raises_a_real_error")
def _raises_a_real_error():
    raise RuntimeError("a genuine failure")


@_guard_app.task(name="guard.errback")
def _errback(*args, **kwargs):
    """Stands in for ``dispatch.on_pipeline_error`` — the link_error that marks a file ERROR."""
    return None


def _errback_calls_for(task) -> int:
    """Run *task* through celery's real tracer with an errback attached; count errback dispatches.

    ``Backend._call_task_errbacks`` is the single point every errback dispatch funnels through
    (``mark_as_failure`` calls it when ``request.errbacks`` is set), so spying there counts
    dispatches without needing a broker to actually deliver them.
    """
    tracer = build_tracer(task.name, task, app=_guard_app, eager=False, propagate=False)
    request = {
        "id": f"guard-{task.name}",
        "errbacks": [_errback.s()],
        "delivery_info": {},
        "called_directly": False,
    }
    with patch.object(type(task.backend), "_call_task_errbacks") as spy:
        tracer(request["id"], (), {}, request)
        return spy.call_count


@pytest.mark.unit
class TestRejectDoesNotFireErrbacks:
    """Contract 1. ``dispatch.py`` attaches ``on_pipeline_error`` as ``link_error`` to every
    pipeline chain, and that callback marks the file ERROR and notifies the user. A graceful
    shutdown must not trip it."""

    def test_a_reject_does_not_dispatch_the_link_error_callback(self):
        assert _errback_calls_for(_rejects_with_requeue) == 0, (
            "celery dispatched the link_error callback for a Reject. In this app that callback "
            "is dispatch.on_pipeline_error, which marks the file ERROR and sends the user a "
            "failure notification — so every graceful `./opentr.sh stop` would now present as "
            "the crash #809 exists to eliminate. Check celery's trace.py: the `except Reject` "
            "arm must stay separate from on_error/handle_failure."
        )

    def test_a_real_failure_still_dispatches_it(self):
        """THE CONTROL, and this test is worthless without it: if the spy were mis-targeted, or
        errbacks never dispatched at all in this harness, the test above would pass while
        measuring nothing."""
        assert _errback_calls_for(_raises_a_real_error) == 1, (
            "the harness never dispatches errbacks at all, so the Reject assertion above "
            "proves nothing — fix the harness before trusting it"
        )


class _RecordingPipe:
    """A redis pipeline stand-in backed by a REAL deque.

    Deliberately not a ``MagicMock``: the property under test is the ORDER the aborted task
    ends up in, and ``pipe.lpush.assert_called_once()`` asserts that kombu called a method, not
    that the message is next to be picked up. With a real deque the assertions below read the
    actual queue contents, so they would survive kombu switching to any other head-insert.
    """

    def __init__(self, queue):
        self.queue = queue

    def lpush(self, key, value):
        self.queue.appendleft(value)

    def rpush(self, key, value):
        self.queue.append(value)


@pytest.mark.unit
class TestKombuRedisActuallyRequeues:
    """Contract 2. ``Reject(requeue=True)`` is only a redelivery because kombu's Redis transport
    implements it as one.

    Driven through the installed kombu's REAL ``QoS.reject`` and ``Channel._do_restore_message``
    against a real deque, rather than against a live broker: an integration test here would skip
    whenever the stack is down — exactly when a regression would slip through unnoticed.
    """

    _WAITING = "a-job-already-queued"

    @staticmethod
    def _channel(kombu_redis):
        return kombu_redis.Channel.__new__(kombu_redis.Channel)

    def _restore_into(self, kombu_redis, queue, *, leftmost: bool) -> None:
        """Run kombu's real restore against *queue*, with its redis lookups neutralised."""
        with (
            patch.object(kombu_redis.Channel, "_lookup", return_value=["q"]),
            patch.object(kombu_redis.Channel, "_get_message_priority", return_value=0),
            patch.object(kombu_redis.Channel, "_q_for_pri", return_value="q"),
        ):
            self._channel(kombu_redis)._do_restore_message(
                {"abort": "payload"}, "ex", "rk", _RecordingPipe(queue), leftmost=leftmost
            )

    def test_a_requeued_abort_is_the_next_task_picked_up_not_the_last(self):
        """The user-visible property, end to end through kombu's own code.

        ``reject()`` decides the flag; ``_do_restore_message`` acts on it. This drives BOTH, so
        a change to either half fails here. An aborted transcription restored to the TAIL would
        sit behind every queued job on a busy worker — hours, on this deployment — which is why
        the position is asserted rather than the method name.
        """
        from kombu.transport import redis as kombu_redis

        qos = kombu_redis.QoS.__new__(kombu_redis.QoS)
        qos._delivered = {"tag-1": MagicMock()}
        qos.channel = MagicMock()

        with (
            patch.object(kombu_redis.QoS, "restore_by_tag") as restore,
            patch.object(kombu_redis.virtual.QoS, "ack"),
        ):
            qos.reject("tag-1", requeue=True)

        assert restore.call_count == 1, (
            "QoS.reject(requeue=True) did not restore the message at all — the aborted "
            "transcription would be acked and lost"
        )
        # bool(): .kwargs.get is untyped, and an un-narrowed Any here would let a future
        # kwargs rename silently pass None through as "restore to the tail".
        leftmost_chosen = bool(restore.call_args.kwargs.get("leftmost"))

        queue = deque([self._WAITING])
        self._restore_into(kombu_redis, queue, leftmost=leftmost_chosen)

        assert len(queue) == 2, "the rejected message was not put back on the queue"
        assert queue[0] != self._WAITING, (
            "the requeued abort landed BEHIND the work already queued. It is still delivered, "
            "so nothing looks broken — but on a busy worker the interrupted transcription now "
            "waits out the whole backlog before it resumes."
        )

    def test_a_non_requeue_reject_leaves_the_queue_untouched(self):
        """The control. A transport that restored on BOTH branches would pass the test above
        while turning `requeue=False` into an infinite redelivery loop."""
        from kombu.transport import redis as kombu_redis

        qos = kombu_redis.QoS.__new__(kombu_redis.QoS)
        qos._delivered = {"tag-2": MagicMock()}
        qos.channel = MagicMock()

        with (
            patch.object(kombu_redis.QoS, "restore_by_tag") as restore,
            patch.object(kombu_redis.QoS, "_remove_from_indices"),
            patch.object(kombu_redis.virtual.QoS, "ack"),
        ):
            qos.reject("tag-2", requeue=False)

        # Replay whatever reject() decided against the real queue, exactly as the requeue=True
        # test does — so this asserts the resulting QUEUE, not a call count. A transport that
        # restored here would show up as a grown queue.
        queue = deque([self._WAITING])
        if restore.call_count:
            self._restore_into(
                kombu_redis, queue, leftmost=bool(restore.call_args.kwargs.get("leftmost"))
            )

        assert list(queue) == [self._WAITING], (
            "reject(requeue=False) put the message back on the queue. A task dropped on purpose "
            "would then be redelivered forever, and the requeue=True assertion above would "
            "prove nothing, since both branches would requeue."
        )

    def test_the_two_restore_positions_are_genuinely_different(self):
        """Guard the guard: if ``leftmost`` were ignored, both branches would produce the same
        queue and the head assertion above would hold no matter what kombu did."""
        from kombu.transport import redis as kombu_redis

        head_queue = deque([self._WAITING])
        tail_queue = deque([self._WAITING])
        self._restore_into(kombu_redis, head_queue, leftmost=True)
        self._restore_into(kombu_redis, tail_queue, leftmost=False)

        assert list(head_queue)[0] != list(tail_queue)[0]
        assert list(head_queue)[-1] == self._WAITING
        assert list(tail_queue)[0] == self._WAITING

    def test_the_installed_kombu_still_exposes_this_mechanism(self):
        """Guard the guard. Every test above patches names on ``kombu.transport.redis``; if a
        bump renamed or removed one, ``patch.object`` would raise — but a rename to a
        *compatible* shape could still leave the abort silently un-requeued. Assert the pieces
        exist by name, so the failure says WHICH one moved."""
        from kombu.transport import redis as kombu_redis

        missing = [
            name
            for name, owner in (
                ("reject", kombu_redis.QoS),
                ("restore_by_tag", kombu_redis.QoS),
                ("_do_restore_message", kombu_redis.Channel),
                ("_restore_at_beginning", kombu_redis.Channel),
            )
            if not hasattr(owner, name)
        ]

        assert not missing, (
            f"kombu's Redis transport no longer exposes {missing}. Reject(requeue=True) is how "
            "#809 avoids losing an aborted transcription — re-derive the redelivery path in "
            "the installed kombu before trusting it."
        )
