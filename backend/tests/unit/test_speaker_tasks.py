"""Tests for ``app/tasks/speaker_tasks.py`` (issue #474).

This module is a **re-export shim, not dead code** — ``app/tasks/CLAUDE.md`` documents it
explicitly: "Keeps legacy task-name routing alive; must stay in celery_app's ``include=``
list." Re-verified here with fresh eyes rather than trusting that comment: live callers
still import task objects through this module rather than their real implementation
module —

- ``app/api/endpoints/summarization.py`` and ``files/management.py``/``files/reprocess.py``
  → ``identify_speakers_llm_task``
- ``app/api/endpoints/transcript_segments.py`` → ``update_speaker_embedding_on_reassignment``
- ``app/api/endpoints/speakers.py`` → ``process_speaker_update_background``
- ``app/services/task_recovery_service.py`` and ``app/tasks/speaker_attribute_task.py`` →
  ``identify_speakers_llm_task``

So a break here (a stale re-export, a rename in the origin module not mirrored, or the
module falling out of ``include=``) would silently misroute six call sites' dispatches
rather than raise an ImportError anyone would notice at review time.

Two things pinned:

1. **Each re-exported name is the exact same object as the real implementation** — not a
   stale copy from before a rename. Celery's ``@app.task`` decorator returns a
   ``celery.local.PromiseProxy``, and a plain ``from module import name`` re-export binds
   the *same* proxy object, so ``speaker_tasks.X is origin_module.X`` must hold with ``is``,
   not just an equal ``.name``.
2. **The registered Celery task name each proxy resolves to is the one production code and
   the admin/revocation paths actually key on** — asserted against ``celery_app.tasks``,
   the live registry, not a second hardcoded copy of the string that could drift
   independently of the source. Note ``celery_app.tasks[name] == proxy`` (not ``is``):
   the registry holds the resolved ``Task`` instance while the module attribute is the lazy
   proxy wrapping it — different objects, delegated equality.
3. The module stays in ``celery_app``'s ``include=`` list, per the explicit warning comment
   above it in ``core/celery.py``.
"""

from celery.app.task import Task

from app.core.celery import celery_app
from app.tasks import speaker_embedding_task
from app.tasks import speaker_identification_task
from app.tasks import speaker_tasks
from app.tasks import speaker_update_task

_REEXPORTS = [
    ("extract_speaker_embeddings_task", "extract_speaker_embeddings"),
    ("update_speaker_embedding_on_reassignment", "update_speaker_embedding_on_reassignment"),
    ("identify_speakers_llm_task", "ai.identify_speakers"),
    ("process_speaker_update_background", "process_speaker_update_background"),
]


def test_all_four_reexports_are_registered_celery_tasks_under_their_real_name():
    """Each re-export is a real Task proxy, registered on celery_app under its real name.

    ``identify_speakers_llm_task``'s registered name (``ai.identify_speakers``) does not
    match its Python attribute name at all — asserting the exact string (not just
    "truthy") is what would catch a rename in the origin module that this shim's import
    line was not updated to follow.
    """
    checked = 0
    for attr_name, expected_task_name in _REEXPORTS:
        obj = getattr(speaker_tasks, attr_name)
        assert isinstance(obj, Task), f"{attr_name} re-export is not a Celery Task proxy"
        assert obj.name == expected_task_name, (
            f"{attr_name}.name is {obj.name!r}, expected {expected_task_name!r} — "
            "the shim and the origin module's @celery_app.task(name=...) have drifted"
        )
        assert expected_task_name in celery_app.tasks, (
            f"{expected_task_name!r} is not registered on celery_app.tasks — importing "
            "app.tasks.speaker_tasks no longer triggers registration of the real task"
        )
        assert celery_app.tasks[expected_task_name] == obj
        checked += 1
    assert checked == 4, "expected exactly 4 re-exports to be checked, module list changed"


def test_reexports_are_the_identical_object_as_the_real_implementation_not_a_copy():
    """``from module import name`` binds the identical PromiseProxy — verified with ``is``.

    This is the property that actually matters for the shim: any staleness (an old
    reference captured before a reload, or a hand-copied duplicate function) would show up
    as ``is`` failing while ``.name`` equality still accidentally passed.
    """
    assert (
        speaker_tasks.extract_speaker_embeddings_task
        is speaker_embedding_task.extract_speaker_embeddings_task
    )
    assert (
        speaker_tasks.update_speaker_embedding_on_reassignment
        is speaker_embedding_task.update_speaker_embedding_on_reassignment
    )
    assert (
        speaker_tasks.identify_speakers_llm_task
        is speaker_identification_task.identify_speakers_llm_task
    )
    assert (
        speaker_tasks.process_speaker_update_background
        is speaker_update_task.process_speaker_update_background
    )


def test_the_shim_module_stays_in_celery_apps_include_list():
    """Regression guard for the explicit warning comment in core/celery.py / tasks/CLAUDE.md.

    Celery's ``include=`` list is what makes a fresh worker process import this module (and
    therefore its three origin modules) at startup at all; dropping the entry would not
    break the direct-import call sites checked above under pytest (they import it
    explicitly), but would leave a production worker that only ever imports
    ``app.tasks.speaker_tasks`` transitively through *this* file with nothing forcing that
    import to happen.
    """
    assert "app.tasks.speaker_tasks" in celery_app.conf.include
