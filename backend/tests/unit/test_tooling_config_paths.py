"""Tooling config must not name paths or modules that match nothing.

A ruff `per-file-ignores` key, a pre-commit `files:`/`exclude:` prefix or a mypy override that
matches nothing is dead config: it looks like a rule, it enforces nothing, and it hides the
day the thing it was written for was renamed or deleted. This is the same failure mode as a
detector that matches nothing, so it is checked mechanically.

Scope (decisions, so the next reader does not re-litigate them):

* Ruff: every `per-file-ignores` / `extend-per-file-ignores` key and every `include` /
  `extend-include` / `exclude` / `extend-exclude` entry anywhere under `[tool.ruff]` must match
  at least one git-tracked file. Paths are relative to the directory holding that
  pyproject.toml (the root file is repo-relative, `backend/pyproject.toml` is relative to
  `backend/`). A pattern with no `/` matches a basename or directory name at any depth, as in
  ruff; an exclude may also match through an ancestor directory.
* Mypy: every `[[tool.mypy.overrides]]` module must resolve. A first-party module (its top
  segment is a directory or `.py` under a `mypy_path` root) must exist down to the full path.
  Anything else must be a registered third-party prefix whose distribution is listed in
  `backend/requirements*.txt`. Whether a third-party override is *used* in a given mypy run
  (`whisperx.*` is reported "unused" when nothing imports it) depends on the files mypy is
  handed, not on the tree, so that is deliberately out of scope here.
* pre-commit: every literal path prefix of every `files:` / `exclude:` regex must be a prefix of
  a tracked path (or exactly a tracked path when the regex pins the end with `$`).

Config that cannot be parsed fails the test; there is no "skip".
"""

import fnmatch
import re
import subprocess
import tomllib
from collections.abc import Iterator
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECTS = [REPO_ROOT / "pyproject.toml", REPO_ROOT / "backend" / "pyproject.toml"]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

# Entries that are stale on today's tree. Ratchet: each must be removed from this table in the
# same change that fixes the config, and the test fails if one stops being stale. Key format is
# `<kind>::<config file relative to repo root>::<entry>`.
KNOWN_STALE: dict[str, str] = {}

# mypy top-level package -> distribution that provides it (must appear in requirements).
THIRD_PARTY_DISTS = {
    "celery": "celery",
    "whisperx": "whisperx",
    "pyannote": "pyannote.audio",
    "minio": "minio",
    "opensearchpy": "opensearch-py",
}


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return [p for p in out.split("\0") if p]


def _load_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- ruff globs


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a ruff/gitignore-style glob: `**/` = zero or more dirs, `*` stays in a segment."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        elif glob[i] == "[":
            j = glob.find("]", i + 1)
            if j == -1:
                out.append(re.escape(glob[i]))
                i += 1
            else:
                out.append(glob[i : j + 1])
                i = j + 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out))


def glob_matches(pattern: str, rel_path: str) -> bool:
    """True if `pattern` selects `rel_path` or any ancestor directory of it."""
    pattern = pattern.removeprefix("./").rstrip("/")
    parts = rel_path.split("/")
    if "/" not in pattern:
        return any(fnmatch.fnmatchcase(part, pattern) for part in parts)
    regex = _glob_to_regex(pattern.lstrip("/"))
    return any(regex.fullmatch("/".join(parts[: n + 1])) for n in range(len(parts)))


_RUFF_LIST_KEYS = {"include", "extend-include", "exclude", "extend-exclude"}
_RUFF_TABLE_KEYS = {"per-file-ignores", "extend-per-file-ignores"}


def _ruff_patterns(node: dict, trail: str = "tool.ruff") -> Iterator[tuple[str, str]]:
    for key, value in node.items():
        if (key in _RUFF_LIST_KEYS and isinstance(value, list)) or (
            key in _RUFF_TABLE_KEYS and isinstance(value, dict)
        ):
            for item in value:
                yield f"{trail}.{key}", str(item)
        elif isinstance(value, dict):
            yield from _ruff_patterns(value, f"{trail}.{key}")


def stale_ruff_entries(pyproject: Path, tracked: Sequence[str], repo_root: Path) -> list[str]:
    ruff = _load_toml(pyproject).get("tool", {}).get("ruff", {})
    base = pyproject.parent.resolve().relative_to(repo_root.resolve()).as_posix()
    prefix = "" if base == "." else base + "/"
    under = [t[len(prefix) :] for t in tracked if t.startswith(prefix)]
    cfg = (prefix + pyproject.name).removeprefix("./")
    return [
        f"ruff::{cfg}::{where}::{pattern}"
        for where, pattern in _ruff_patterns(ruff)
        if not any(glob_matches(pattern, rel) for rel in under)
    ]


# ---------------------------------------------------------------- mypy overrides


def _requirement_names(requirements: Sequence[str]) -> set[str]:
    names = set()
    for line in requirements:
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m and not line.lstrip().startswith("#"):
            names.add(re.sub(r"[-_.]+", "-", m.group(1)).lower())
    return names


def stale_mypy_entries(
    pyproject: Path, tracked: Sequence[str], repo_root: Path, requirements: Sequence[str]
) -> list[str]:
    mypy = _load_toml(pyproject).get("tool", {}).get("mypy", {})
    base = pyproject.parent.resolve().relative_to(repo_root.resolve()).as_posix()
    cfg = (("" if base == "." else base + "/") + pyproject.name).removeprefix("./")
    roots = (
        [(Path(base) / p).as_posix().removeprefix("./") for p in mypy.get("mypy_path", ".")]
        if isinstance(mypy.get("mypy_path"), list)
        else [(Path(base) / mypy.get("mypy_path", ".")).as_posix().removeprefix("./")]
    )
    roots = [r if r != "." else "" for r in roots]
    declared = _requirement_names(requirements)
    stale = []
    for override in mypy.get("overrides", []):
        modules = override.get("module", [])
        for module in [modules] if isinstance(modules, str) else modules:
            segments = [s for s in module.split(".") if s not in ("*", "")]
            if not segments:
                stale.append(f"mypy::{cfg}::{module}")
                continue
            rel = "/".join(segments)

            def exists(root: str, rel: str = rel) -> bool:
                stem = f"{root}/{rel}" if root else rel
                return any(t == f"{stem}.py" or t.startswith(f"{stem}/") for t in tracked)

            top_is_first_party = any(exists(r, segments[0]) for r in roots)
            if top_is_first_party:
                ok = any(exists(r, rel) for r in roots)
            else:
                dist = THIRD_PARTY_DISTS.get(segments[0])
                ok = dist is not None and re.sub(r"[-_.]+", "-", dist).lower() in declared
            if not ok:
                stale.append(f"mypy::{cfg}::{module}")
    return stale


# ---------------------------------------------------------------- pre-commit regexes


def _split_top(pattern: str) -> list[str]:
    parts, depth, cur, i = [], 0, "", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            cur += pattern[i : i + 2]
            i += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "|" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    parts.append(cur)
    return parts


def _group_end(pattern: str, start: int) -> int:
    depth, i = 0, start
    while i < len(pattern):
        if pattern[i] == "\\":
            i += 2
            continue
        depth += pattern[i] == "("
        depth -= pattern[i] == ")"
        if depth == 0:
            return i
        i += 1
    raise ValueError(f"unbalanced group in {pattern!r}")


def literal_prefixes(pattern: str) -> set[tuple[str, bool]]:
    """Expand a pre-commit path regex to `(literal, closed)` pairs.

    `closed` means the regex pins the end right after the literal (`$`), so the literal is a
    whole path; otherwise it is only a prefix and stops at the first non-literal construct.
    Alternatives that are not anchored with `^` can match anywhere in a path, so they say
    nothing checkable and are skipped. Raises ValueError on unbalanced groups, so the caller
    fails closed.
    """
    # an alternative that ends while still "cont" is a plain prefix match, i.e. open
    return {(lit, state == "closed") for lit, state in _expand(pattern, anchored_only=True)}


def _expand(pattern: str, *, anchored_only: bool) -> list[tuple[str, str]]:
    """States: cont (more regex may follow), open (stopped at a metachar), closed (`$`)."""
    results: list[tuple[str, str]] = []
    for alt in _split_top(pattern):
        if anchored_only:
            if not alt.startswith("^"):
                continue
            alt = alt[1:]
        partial = [("", "cont")]
        i = 0
        while i < len(alt) and any(state == "cont" for _, state in partial):
            c = alt[i]
            if c == "\\" and i + 1 < len(alt) and alt[i + 1] in ".-/_":
                step, lit_add, sub = 2, alt[i + 1], None
            elif c == "(":
                end = _group_end(alt, i)
                step, lit_add, sub = (
                    end + 1 - i,
                    "",
                    _expand(alt[i + 1 : end].removeprefix("?:"), anchored_only=False),
                )
            elif c == "$":
                step, lit_add, sub = 1, "", [("", "closed")]
            elif c in ".*+?[]{}^":
                step, lit_add, sub = 1, "", [("", "open")]
            else:
                step, lit_add, sub = 1, c, None
            nxt = []
            for lit, state in partial:
                if state != "cont":
                    nxt.append((lit, state))
                elif sub is None:
                    nxt.append((lit + lit_add, "cont"))
                else:
                    nxt.extend((lit + sl, ss) for sl, ss in sub)
            partial = nxt
            i += step
        results.extend(partial)
    return results


def _regex_fields(node: object, trail: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("files", "exclude") and isinstance(value, str):
                yield trail or "<top>", value
            else:
                yield from _regex_fields(
                    value, f"{trail}/{node.get('id', key)}" if key == "hooks" else trail
                )
    elif isinstance(node, list):
        for item in node:
            hid = item.get("id") if isinstance(item, dict) else None
            yield from _regex_fields(item, f"{trail}/{hid}" if hid else trail)


def stale_precommit_entries(config: Path, tracked: Sequence[str]) -> list[str]:
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data.get("repos"), f"{config} did not parse as a config"
    cfg = config.name
    stale = []
    for hook, regex in _regex_fields(data):
        try:
            re.compile(regex)
            prefixes = literal_prefixes(regex)
        except (re.error, ValueError) as exc:
            stale.append(f"precommit::{cfg}::{regex} (unparseable: {exc})")
            continue
        for lit, closed in sorted(prefixes):
            if not lit:
                continue
            hit = (
                any(t == lit for t in tracked)
                if closed
                else any(t.startswith(lit) for t in tracked)
            )
            if not hit:
                stale.append(f"precommit::{cfg}::{lit}")
    return sorted(set(stale))


# ---------------------------------------------------------------- the tests


def _requirements() -> list[str]:
    lines: list[str] = []
    for req in sorted((REPO_ROOT / "backend").glob("requirements*.txt")):
        lines += req.read_text(encoding="utf-8").splitlines()
    return lines


def _all_findings() -> set[str]:
    tracked = tracked_files()
    found: set[str] = set()
    for pyproject in PYPROJECTS:
        found.update(stale_ruff_entries(pyproject, tracked, REPO_ROOT))
        found.update(stale_mypy_entries(pyproject, tracked, REPO_ROOT, _requirements()))
    found.update(stale_precommit_entries(PRECOMMIT_CONFIG, tracked))
    return found


def test_no_unexpected_stale_tooling_config() -> None:
    new = _all_findings() - set(KNOWN_STALE)
    assert not new, "tooling config entries that match nothing:\n  " + "\n  ".join(sorted(new))


def test_known_stale_entries_are_still_stale() -> None:
    fixed = set(KNOWN_STALE) - _all_findings()
    assert not fixed, (
        "these KNOWN_STALE entries are no longer stale; delete them from the table:\n  "
        + "\n  ".join(sorted(fixed))
    )


def test_known_stale_entries_carry_a_reason() -> None:
    assert all(len(reason) > 40 for reason in KNOWN_STALE.values())


# ------------------------------------------------ the checkers can fail (planted configs)

_TRACKED = ["app/a.py", "tests/t.py", "scripts/run.sh", "backend/app/x.py"]


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_ruff_planted_per_file_ignore_is_flagged(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        "pyproject.toml",
        '[tool.ruff.lint.per-file-ignores]\n"**/tests/**" = ["S101"]\n"nope/**/*.py" = ["E402"]\n'
        '"a.py" = ["F401"]\n[tool.ruff]\nextend-exclude = ["missing_dir"]\n',
    )
    stale = stale_ruff_entries(cfg, _TRACKED, tmp_path)
    assert sorted(s.rsplit("::", 1)[1] for s in stale) == ["missing_dir", "nope/**/*.py"]


def test_mypy_planted_overrides_are_flagged(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        "pyproject.toml",
        '[tool.mypy]\nmypy_path = "backend"\n[[tool.mypy.overrides]]\n'
        'module = ["app.*", "app.gone.*", "celery.*", "ghostlib.*"]\nignore_errors = true\n',
    )
    stale = stale_mypy_entries(cfg, _TRACKED, tmp_path, ["celery==5.0"])
    assert sorted(s.rsplit("::", 1)[1] for s in stale) == ["app.gone.*", "ghostlib.*"]


def test_precommit_planted_prefix_is_flagged(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        ".pre-commit-config.yaml",
        "repos:\n- repo: local\n  hooks:\n  - id: h\n    files: ^(backend|nope)/\n"
        "    exclude: ^scripts/run\\.sh$|^scripts/gone\\.sh$\n",
    )
    stale = stale_precommit_entries(cfg, _TRACKED)
    assert sorted(s.rsplit("::", 1)[1] for s in stale) == ["nope/", "scripts/gone.sh"]


def test_unparseable_precommit_config_fails_closed(tmp_path: Path) -> None:
    cfg = _write(tmp_path, ".pre-commit-config.yaml", "just: a string\n")
    with pytest.raises(AssertionError):
        stale_precommit_entries(cfg, _TRACKED)


def test_literal_prefixes_handle_nested_alternation() -> None:
    got = literal_prefixes(r"^(a\.txt|b/(c|d)\.py)$|^e/.*\.md$")
    assert got == {("a.txt", True), ("b/c.py", True), ("b/d.py", True), ("e/", False)}
