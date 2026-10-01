"""Speaker-attribute (gender) detection must work on air-gapped installs.

``detect_speaker_attributes`` loads a wav2vec2 gender classifier from the Hugging
Face cache. It is routed to the ``cpu`` queue, served by ``celery-cpu-worker`` — a
service that mounted no Hugging Face cache at all. Offline stacks run with
``HF_HUB_OFFLINE=1`` (``local_files_only``), so the model could not load and
attribute detection failed on every file; on a networked stack it silently
re-downloaded ~380 MB into the container layer instead.

Both halves are derived, not hand-listed:

* the queues come from ``celery_app.conf.task_routes`` for every task that loads the
  attribute model (plus the lite-mode reroute of the GPU-preferred ones);
* the serving services come from each compose service's ``-Q`` flag, evaluated on
  the base file merged with each overlay the way ``docker compose -f a -f b`` does
  (``volumes`` merge by container target, ``command`` is replaced).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE = "docker-compose.yml"

#: Tasks whose body loads ``SpeakerAttributeService`` / ``GenderModelAdapter``.
ATTRIBUTE_TASKS = (
    "detect_speaker_attributes",
    "migrate_speaker_attributes",
    "detect_speaker_attributes_batch",
    "analyze_speakers_combined_batch",
    "migrate_speakers_combined",
)

#: Matched on the tail: ``Dockerfile.blackwell`` runs as ``user``, not ``appuser``.
HF_CACHE_SUFFIX = ".cache/huggingface"

#: Compose stacks a user can actually run, as ordered ``-f`` chains after the base.
CHAINS = (
    (),
    ("docker-compose.override.yml",),
    ("docker-compose.prod.yml",),
    ("docker-compose.prod.yml", "docker-compose.offline.yml"),
    ("docker-compose.offline.yml",),
    ("docker-compose.prod.yml", "docker-compose.lite.yml"),
    ("docker-compose.override.yml", "docker-compose.lite.yml"),
    ("docker-compose.prod.yml", "docker-compose.gpu.yml"),
    ("docker-compose.prod.yml", "docker-compose.blackwell.yml"),
    ("docker-compose.override.yml", "docker-compose.blackwell.yml"),
    ("docker-compose.prod.yml", "docker-compose.gpu-scale.yml"),
    ("docker-compose.override.yml", "docker-compose.gpu-scale.yml"),
    ("docker-compose.prod.yml", "docker-compose.offline.yml", "docker-compose.gpu-scale.yml"),
    ("docker-compose.prod.yml", "docker-compose.gpu-split.yml"),
    ("docker-compose.override.yml", "docker-compose.gpu-split.yml"),
    ("docker-compose.override.yml", "docker-compose.bench.yml"),
    ("docker-compose.override.yml", "docker-compose.bench-gpu.yml"),
    ("docker-compose.prod.yml", "docker-compose.nas.yml"),
    ("docker-compose.prod.yml", "docker-compose.watch.yml"),
)

_INTERPOLATION = re.compile(r"\$\{[^}]*\}")


def _load(name: str) -> dict[str, dict]:
    yaml = pytest.importorskip("yaml", reason="PyYAML parses the compose files")
    path = REPO_ROOT / name
    if not path.is_file():
        pytest.skip(f"{name} is not present in this checkout")
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")).get("services") or {})


def _target(volume: str) -> str | None:
    parts = _INTERPOLATION.sub("", volume).split(":")
    return parts[1].strip() if len(parts) >= 2 else None


def _merge(chain: tuple[str, ...]) -> dict[str, dict]:
    """Approximate ``docker compose -f base -f ...``: volumes by target, command replaced."""
    merged: dict[str, dict] = {}
    for name in (BASE, *chain):
        for svc_name, svc in _load(name).items():
            slot = merged.setdefault(svc_name, {"volumes": {}, "command": None})
            for vol in svc.get("volumes") or []:
                if isinstance(vol, str) and (target := _target(vol)):
                    slot["volumes"][target] = vol
            if svc.get("command") is not None:
                command = svc["command"]
                slot["command"] = command if isinstance(command, str) else " ".join(command)
    return merged


def _queues(command: str | None) -> set[str]:
    match = re.search(r"-Q\s+([\w,\-]+)", command or "")
    return set(match.group(1).split(",")) if match else set()


def _attribute_queues() -> set[str]:
    from app.core.celery import celery_app
    from app.core.constants import gpu_preferred_queue

    routes = celery_app.conf.task_routes or {}
    queues = set()
    for task in ATTRIBUTE_TASKS:
        assert task in routes, f"{task!r} has no explicit route — which worker runs it?"
        queues.add(str(routes[task]["queue"]))
    queues.add(gpu_preferred_queue("lite"))
    return queues


def test_attribute_queue_derivation_finds_cpu_and_gpu() -> None:
    """Guard on the guard: an empty derivation would pass every assertion below."""
    assert {"cpu", "gpu"} <= _attribute_queues()


@pytest.mark.parametrize(
    "chain",
    CHAINS,
    ids=lambda c: "+".join(x.removeprefix("docker-compose.").removesuffix(".yml") for x in c)
    or "base",
)
def test_every_attribute_worker_mounts_the_model_cache(chain: tuple[str, ...]) -> None:
    queues = _attribute_queues()
    services = _merge(chain)
    serving = {n: s for n, s in services.items() if _queues(s["command"]) & queues}
    assert "celery-cpu-worker" in serving, "nothing serves the cpu queue — derivation broke"

    missing = [
        n for n, s in serving.items() if not any(t.endswith(HF_CACHE_SUFFIX) for t in s["volumes"])
    ]
    assert not missing, (
        f"{missing} run speaker-attribute tasks but mount no *{HF_CACHE_SUFFIX}; the gender "
        f"model cannot load offline (HF_HUB_OFFLINE=1) and is re-downloaded at runtime online."
    )


def _download_groups() -> tuple[set[str], set[str]]:
    """``(group names, string literals inside the speaker-attributes downloader)``."""
    tree = ast.parse((REPO_ROOT / "scripts" / "download-models.py").read_text(encoding="utf-8"))
    groups: set[str] = set()
    func_name = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DOWNLOAD_GROUPS" for t in node.targets
        ):
            assert isinstance(node.value, ast.Dict)
            for key, value in zip(node.value.keys, node.value.values, strict=True):
                assert isinstance(key, ast.Constant)
                assert isinstance(key.value, str)
                groups.add(key.value)
                if key.value == "speaker-attributes":
                    assert isinstance(value, ast.Name)
                    func_name = value.id
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            literals = {
                c.value
                for c in ast.walk(node)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            }
    return groups, literals


def test_gender_model_is_in_the_download_manifest() -> None:
    from app.services.speaker_attribute_service import MODEL_NAME

    groups, literals = _download_groups()
    assert "speaker-attributes" in groups
    assert MODEL_NAME in literals, (
        f"download-models.py's speaker-attributes group does not fetch {MODEL_NAME!r}, "
        f"the model the CPU worker loads."
    )


@pytest.mark.parametrize(
    "script", ("scripts/download-models.sh", "scripts/build-offline-package.sh")
)
def test_standard_download_and_offline_bundle_fetch_every_group(script: str) -> None:
    """Both run ``download-models.py`` with no ``--only``, so the gender model is included."""
    text = (REPO_ROOT / script).read_text(encoding="utf-8")
    invocations = [
        line for line in text.splitlines() if "download-models.py" in line and "python" in line
    ]
    assert invocations, f"{script} no longer runs download-models.py"
    assert not any("--only" in line for line in invocations), (
        f"{script} restricts the download to selected groups; the gender model would be dropped."
    )
