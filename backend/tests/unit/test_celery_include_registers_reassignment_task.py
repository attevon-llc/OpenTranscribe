"""The reassignment task must be registered through ``include``, not the re-export shim."""

from app.core.celery import celery_app

REASSIGNMENT_MODULE = "app.tasks.speaker_embedding_reassignment_task"


def test_reassignment_module_is_in_celery_include():
    assert REASSIGNMENT_MODULE in celery_app.conf.include
