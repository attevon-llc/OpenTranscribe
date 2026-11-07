"""A setting read from the environment but overwritten on every task path is a lie.

``TranscriptionConfig.from_environment`` builds a config from env vars, then every
transcription task applies a per-job ``overrides`` dict on top. A field that the env feeds
AND that every task's ``overrides`` sets can never be changed from the environment:
``VAD_MIN_SILENCE_MS`` sat in ``.env.example`` and three compose blocks for months while
the value it fed was replaced by a DB-backed default before the first decode (issue #566,
#1206). ``test_env_example_coverage`` cannot see this: the key IS read, just never used.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = REPO_ROOT / "backend" / "app"
CONFIG_SRC = APP_DIR / "transcription" / "config.py"
PIPELINES_SRC = APP_DIR / "tasks" / "transcription" / "pipelines.py"

_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_ENV_CALLS = {"getenv", "get", "env_int", "env_float", "env_bool"}


def env_fed_fields(source: str, function: str) -> dict[str, set[str]]:
    """``{constructor keyword: env names}`` for the ``cls(...)`` call inside ``function``."""
    result: dict[str, set[str]] = {}
    for fn in ast.walk(ast.parse(source)):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == function):
            continue
        for call in ast.walk(fn):
            if not (isinstance(call, ast.Call) and ast.unparse(call.func) == "cls"):
                continue
            for kw in call.keywords:
                if kw.arg is None:
                    continue
                for inner in ast.walk(kw.value):
                    if not isinstance(inner, ast.Call) or not inner.args:
                        continue
                    name = getattr(inner.func, "attr", getattr(inner.func, "id", ""))
                    first = inner.args[0]
                    if (
                        name in _ENV_CALLS
                        and isinstance(first, ast.Constant)
                        and isinstance(first.value, str)
                        and _ENV_NAME.fullmatch(first.value)
                    ):
                        result.setdefault(kw.arg, set()).add(first.value)
    return result


def always_overridden_fields(source: str) -> set[str]:
    """Keywords present in EVERY ``overrides = dict(...)`` literal of a module."""
    sets: list[set[str]] = []
    for node in ast.walk(ast.parse(source)):
        value = None
        if (
            isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", "") == "overrides"
            or isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "overrides" for t in node.targets)
        ):
            value = node.value
        if isinstance(value, ast.Call) and ast.unparse(value.func) == "dict":
            sets.append({kw.arg for kw in value.keywords if kw.arg})
    return set.intersection(*sets) if sets else set()


def shadowed_env_reads(config_source: str, pipelines_source: str) -> dict[str, str]:
    """``{env name: field}`` for env reads whose field every task overrides."""
    overridden = always_overridden_fields(pipelines_source)
    return {
        env: field
        for field, envs in env_fed_fields(config_source, "from_environment").items()
        if field in overridden
        for env in envs
    }


def test_no_env_read_is_overwritten_on_every_task_path():
    shadowed = shadowed_env_reads(
        CONFIG_SRC.read_text(encoding="utf-8"), PIPELINES_SRC.read_text(encoding="utf-8")
    )
    assert not shadowed, (
        f"env reads that every task's overrides replace, so the env value is never used: "
        f"{shadowed}. Delete the read, or stop overriding the field."
    )


def test_shadowed_names_are_not_advertised():
    """The deleted names must not creep back into the template or a compose file."""
    dead = (
        "VAD_THRESHOLD",
        "VAD_MIN_SILENCE_MS",
        "VAD_MIN_SPEECH_MS",
        "VAD_SPEECH_PAD_MS",
        "WHISPER_HALLUCINATION_THRESHOLD",
        "WHISPER_REPETITION_PENALTY",
        "SOURCE_LANGUAGE",
    )
    files = [REPO_ROOT / ".env.example", *REPO_ROOT.glob("docker-compose*.yml")]
    assert len(files) > 5, "compose glob found too few files to mean anything"
    mentions = [
        f"{path.name}: {name}"
        for path in files
        for name in dead
        if re.search(rf"\b{name}\b", path.read_text(encoding="utf-8"))
    ]
    assert not mentions, f"removed names are advertised again: {mentions}"


def test_the_scanner_fires_on_a_shadowed_read_and_not_on_a_live_one():
    """Guard the guard: a scanner that matches nothing would pass the real check forever."""
    config = (
        "class C:\n"
        "    @classmethod\n"
        "    def from_environment(cls, **o):\n"
        "        return cls(vad=int(os.getenv('SHADOWED_ONE', '1')),\n"
        "                   beam=int(os.getenv('LIVE_ONE', '5')))\n"
    )
    pipelines = "overrides: dict = dict(vad=1, other=2)\nsecond = 3\noverrides = dict(vad=2)\n"
    assert shadowed_env_reads(config, pipelines) == {"SHADOWED_ONE": "vad"}
    # An override set only on SOME paths does not shadow the env on the others.
    partial = "overrides = dict(vad=1)\noverrides = dict(other=2)\n"
    assert shadowed_env_reads(config, partial) == {}
