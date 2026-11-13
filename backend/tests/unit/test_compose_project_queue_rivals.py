"""``rivals_from_commands`` finds every other consumer of a queue from real container commands.

The diar-native live tests used a hardcoded rival service name and missed the default
``celery-worker`` (``-Q gpu``), so a job could be served by a worker the test was not
watching (v070verify gate, issue #711).
"""

from tests.compose_project import consumed_queues
from tests.compose_project import rivals_from_commands

CMDS = {
    "p-celery-worker": ["celery", "-A", "app.core.celery", "worker", "-Q", "gpu", "-E"],
    "p-celery-worker-gpu-scaled": ["celery", "worker", "-Q", "gpu", "-n", "gpu-scaled@%h"],
    "p-celery-cpu-worker": ["celery", "worker", "-Q", "cpu,utility,cpu-transcribe"],
    "p-gpu-diarize": ["celery", "worker", "--queues=gpu-diarize"],
    "p-backend": ["uvicorn", "app.main:app"],
}


def test_default_gpu_worker_is_a_rival_and_cpu_worker_is_not() -> None:
    rivals = rivals_from_commands("p-celery-worker-gpu-scaled", CMDS, {"gpu", "gpu-diarize"})
    assert rivals == ["p-celery-worker", "p-gpu-diarize"]


def test_no_rival_when_alone_on_the_queue() -> None:
    only = {k: v for k, v in CMDS.items() if k != "p-celery-worker"}
    assert rivals_from_commands("p-celery-worker-gpu-scaled", only, {"gpu"}) == []


def test_consumed_queues_parses_both_flag_forms() -> None:
    assert consumed_queues(["worker", "-Q", "a,b"]) == {"a", "b"}
    assert consumed_queues(["worker", "--queues=c"]) == {"c"}
    assert consumed_queues(["uvicorn"]) == set()
