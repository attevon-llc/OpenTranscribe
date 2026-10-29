"""Tests for ``scripts/guards/check_suppressions.py``.

The guard counts suppressions that lack a rule code (and, for ``noqa``, a written reason).
A guard that matches nothing reports a clean tree, so each kind has a must-fire fixture.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "guards" / "check_suppressions.py"
_BASELINE = _REPO / "scripts" / "guards" / "baselines" / "suppressions.txt"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_suppressions_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "line"),
    [
        ("noqa", "x = 1  # noqa\n"),
        ("noqa", "x = 1  # noqa: E501\n"),
        ("noqa", "x = 1  # noqa: BLE001, S110\n"),
        ("noqa", "x = 1  # noqa: S603 # nosec B603\n"),
        ("noqa", "# ruff: noqa\n"),
        ("type-ignore", "x = f()  # type: ignore\n"),
        ("nosec", "x = f()  # nosec\n"),
    ],
)
def test_python_violation_fires(kind: str, line: str) -> None:
    assert guard.count_python(line, "f.py").get(kind) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "x = 1  # noqa: E501 - generated URL, cannot wrap\n",
        "x = 1  # noqa: BLE001, S110 best-effort cleanup must not raise\n",
        "x = f()  # type: ignore[assignment]\n",
        "x = f()  # type: ignore[attr-defined, misc]\n",
        "x = f()  # nosec B603\n",
        "x = f()  # nosec: B603\n",
        "x = 1  # an ordinary comment about noqa or type: ignore in prose\n",
    ],
)
def test_python_compliant_passes(line: str) -> None:
    assert guard.count_python(line, "f.py") == {}


@pytest.mark.unit
def test_python_strings_are_not_comments() -> None:
    source = 's = "x = 1  # noqa"\nt = """\n# type: ignore\n# nosec\n"""\n'
    assert guard.count_python(source, "f.py") == {}


@pytest.mark.unit
def test_python_untokenisable_source_raises() -> None:
    with pytest.raises(guard.bl.ScanError):
        guard.count_python("x = (\n", "broken.py")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "source"),
    [
        ("eslint-disable", "/* eslint-disable */\n"),
        ("eslint-disable", "// eslint-disable-next-line\nlet a;\n"),
        ("eslint-disable", "let a; // eslint-disable-line -- because\n"),
        ("eslint-disable", "<!-- eslint-disable -->\n"),
        ("ts-ignore", "// @ts-ignore\nlet a;\n"),
        ("ts-ignore", "/* @ts-ignore */\n"),
        ("svelte-ignore", "<!-- svelte-ignore -->\n"),
    ],
)
def test_web_violation_fires(kind: str, source: str) -> None:
    assert guard.count_web(source).get(kind) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "// eslint-disable-next-line @typescript-eslint/no-explicit-any\n",
        "/* eslint-disable no-console */\n",
        "// eslint-disable-line svelte/prefer-svelte-reactivity -- reason\n",
        "<!-- svelte-ignore a11y_autofocus -->\n",
        "<!-- svelte-ignore a11y-click-events-have-key-events -->\n",
        "// eslint-enable\n",
        "// @ts-expect-error library types are wrong\n",
    ],
)
def test_web_compliant_passes(source: str) -> None:
    assert guard.count_web(source) == {}


def _tree(tmp_path: Path, files: dict[str, str | bytes]) -> list[str]:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return list(files)


@pytest.mark.unit
def test_scan_keys_and_scope(tmp_path: Path) -> None:
    files = _tree(
        tmp_path,
        {
            "backend/app/a.py": "x = 1  # noqa\ny = 2  # noqa\n",
            "frontend/src/lib/a.ts": "// @ts-ignore\n",
            "frontend/src/lib/ok.ts": "// eslint-disable-next-line no-console\n",
            "docs/out_of_scope.py": "x = 1  # noqa\n",
            "frontend/other/out_of_scope.ts": "// @ts-ignore\n",
        },
    )
    assert guard.scan(tmp_path, files) == {
        "noqa::backend/app/a.py": 2,
        "ts-ignore::frontend/src/lib/a.ts": 1,
    }


@pytest.mark.unit
def test_scan_unreadable_file_fails_closed(tmp_path: Path) -> None:
    files = _tree(tmp_path, {"backend/app/bad.py": b"x = 1  # \xff\xfe\n"})
    with pytest.raises(guard.bl.ScanError):
        guard.scan(tmp_path, files)
    with pytest.raises(guard.bl.ScanError):
        guard.scan(tmp_path, ["backend/app/missing.py"])


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: list[str], *args: str) -> int:
    monkeypatch.setattr(guard, "list_files", lambda _root: files)
    code: int = guard.main(
        ["--root", str(tmp_path), "--baseline", str(tmp_path / "base.txt"), *args]
    )
    return code


@pytest.mark.unit
def test_planted_violation_fails_then_baseline_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _tree(tmp_path, {"backend/app/_probe.py": "x = 1  # noqa\n"})
    assert _run(tmp_path, monkeypatch, files) == 1
    assert (
        _run(
            tmp_path,
            monkeypatch,
            files,
            "--write-baseline",
            "--reason",
            "suppression: reason to be added",
        )
        == 0
    )
    assert _run(tmp_path, monkeypatch, files) == 0
    (tmp_path / "backend/app/_probe.py").write_text(
        "x = 1  # noqa\ny = 1  # noqa\n", encoding="utf-8"
    )
    assert _run(tmp_path, monkeypatch, files) == 1, "growth past the baseline must fail"


@pytest.mark.unit
def test_stale_and_loosened_entries_fail_and_tighten_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = _tree(
        tmp_path,
        {
            "backend/app/a.py": "x = 1  # noqa\ny = 1  # noqa\n",
            "backend/app/b.py": "x = 1  # noqa\n",
        },
    )
    assert (
        _run(
            tmp_path,
            monkeypatch,
            files,
            "--write-baseline",
            "--reason",
            "suppression: reason to be added",
        )
        == 0
    )
    (tmp_path / "backend/app/a.py").write_text("x = 1  # noqa\n", encoding="utf-8")  # loosened
    (tmp_path / "backend/app/b.py").write_text("x = 1\n", encoding="utf-8")  # stale
    assert _run(tmp_path, monkeypatch, files) == 1
    err = capsys.readouterr().err
    assert "below the baseline" in err
    assert "stale baseline entry" in err
    assert _run(tmp_path, monkeypatch, files, "--tighten") == 0
    assert _run(tmp_path, monkeypatch, files) == 0
    assert (tmp_path / "base.txt").read_text(encoding="utf-8").count("\n") == 1


@pytest.mark.unit
def test_unreadable_file_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _tree(tmp_path, {"backend/app/bad.py": b"\xff\xfe"})
    assert _run(tmp_path, monkeypatch, files) == 2


@pytest.mark.unit
def test_real_repo_passes_with_committed_baseline() -> None:
    assert _BASELINE.is_file(), "the committed baseline is missing"
    assert guard.main(["--root", str(_REPO)]) == 0


# (kind, bad file name, bad content, good content): each kind is exercised on disk, alone.
_PER_KIND = [
    ("noqa", "a.py", "x = 1  # noqa\n", "x = 1  # noqa: E501 - generated URL\n"),
    ("noqa", "a.py", "x = 1  # noqa: E501\n", "x = 1  # noqa: E501 - generated URL\n"),
    ("type-ignore", "a.py", "x = f()  # type: ignore\n", "x = f()  # type: ignore[misc]\n"),
    ("nosec", "a.py", "x = f()  # nosec\n", "x = f()  # nosec B603\n"),
    ("eslint-disable", "a.ts", "/* eslint-disable */\n", "/* eslint-disable no-console */\n"),
    (
        "eslint-disable",
        "a.ts",
        "// eslint-disable-next-line\nlet a;\n",
        "// eslint-disable-next-line no-console\nlet a;\n",
    ),
    (
        "eslint-disable",
        "a.svelte",
        "<!-- eslint-disable -->\n",
        "<!-- eslint-disable no-console -->\n",
    ),
    (
        "ts-ignore",
        "a.ts",
        "// @ts-ignore\nlet a;\n",
        "// @ts-expect-error wrong upstream type\nlet a;\n",
    ),
    (
        "svelte-ignore",
        "a.svelte",
        "<!-- svelte-ignore -->\n<p></p>\n",
        "<!-- svelte-ignore a11y_autofocus -->\n<p></p>\n",
    ),
    (
        "svelte-ignore",
        "a.svelte",
        "<!-- svelte-ignore\n-->\n",
        "<!-- svelte-ignore a11y-autofocus -->\n",
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "name", "bad", "good"),
    _PER_KIND,
    ids=[f"{k}-{i}" for i, (k, *_r) in enumerate(_PER_KIND)],
)
def test_each_kind_on_disk_fires_alone_and_stays_clean_when_written_properly(
    tmp_path: Path, kind: str, name: str, bad: str, good: str
) -> None:
    prefix = "backend/app/" if name.endswith(".py") else "frontend/src/lib/"
    rel = prefix + name
    files = _tree(tmp_path, {rel: bad})
    assert guard.scan(tmp_path, files) == {f"{kind}::{rel}": 1}
    (tmp_path / rel).write_text(good, encoding="utf-8")
    assert guard.scan(tmp_path, files) == {}
