"""Issue #886: the project's own licence was invisible to every artifact that is supposed to
report it — the package manifests declared none, and the release SBOM attributed none.

TWO HALVES, BOTH GUARDED HERE.

**Manifests** (#886 acceptance criteria 1 and 2). Before the first half of this fix,
`rg -in "license"` returned zero matches in every first-party `package.json`/`pyproject.toml`
in the tree. They now all declare the same SPDX identifier, and a new manifest added without
one fails here.

**Images** (#886 acceptance criterion 3 — "a regenerated SBOM attributes the licence to the
first-party components"). The manifests alone could never satisfy that, and the reasons were
measured against syft 1.33.0, not assumed:

* The final `nginx:*-alpine` stages of `frontend/Dockerfile.prod` and `docs-site/Dockerfile`
  contain **zero** `package.json` files — `find / -name package.json` inside the published
  `davidamacey/opentranscribe-frontend:v0.5.0` and `-docs:v0.5.0` images returns count 0. Only
  the built static output and `nginx.conf` are copied in.
* syft catalogues **nothing at all** from a `pyproject.toml`. Control: a `pyproject.toml`
  carrying `[project] name/version/license`, scanned both as `dir:` and inside an image,
  produced an EMPTY artifact list both times. (The npm control is the opposite — a bare
  `package.json` in an image IS catalogued with its licence.) `backend/pyproject.toml` is not
  copied into the backend image either, which makes it moot twice over.

So the mechanism is the **OCI annotation** `org.opencontainers.image.licenses`, as a `LABEL`
on the final stage of every production Dockerfile. syft surfaces image labels, and
`scripts/lib/sbom_license.py` (called from `generate_sbom()` in `scripts/security-scan.sh`)
promotes that value into CycloneDX `metadata.component.licenses`. This file is the gate on
the declaration; that script is the gate on it reaching the document.

⚠️ **The identifier is DERIVED from `LICENSE`, never typed here.** A test asserting
`== "AGPL-3.0-only"` against sources that say `AGPL-3.0-only` proves only that the two were
typed on the same day. `_license_family_from_license_file()` reads the licence's own title out
of `LICENSE` and the tests assert every declaration is a **current** SPDX identifier for that
family and that they all agree. That is what caught the real drift this half of the issue
found: four Dockerfiles declared the **deprecated** bare `AGPL-3.0` (withdrawn from the SPDX
list in favour of the `-only`/`-or-later` pair) while the manifests declared `AGPL-3.0-only`,
and `docs-site/Dockerfile` declared nothing at all.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
LICENSE_FILE = REPO_ROOT / "LICENSE"
SECURITY_SCAN_SCRIPT = REPO_ROOT / "scripts" / "security-scan.sh"

#: The OCI annotation a production image declares its licence with.
OCI_LICENSE_LABEL = "org.opencontainers.image.licenses"

#: Licence families whose SPDX identifier is disjunctive — the bare family name is DEPRECATED
#: and a current document must pick `-only` or `-or-later`. Membership of this set is what
#: makes a bare `AGPL-3.0` a failure rather than a synonym.
_DISJUNCTIVE_FAMILIES = frozenset(
    {"AGPL-3.0", "GPL-3.0", "GPL-2.0", "LGPL-3.0", "LGPL-2.1", "LGPL-2.0"}
)

#: Licence title -> SPDX family, matched against the whitespace-normalised head of LICENSE.
#: Ordered longest-first so the AGPL title cannot be swallowed by the GPL one.
_LICENSE_TITLES: tuple[tuple[str, str], ...] = (
    ("gnu affero general public license version 3", "AGPL-3.0"),
    ("gnu lesser general public license version 3", "LGPL-3.0"),
    ("gnu general public license version 3", "GPL-3.0"),
    ("gnu general public license version 2", "GPL-2.0"),
    ("apache license version 2.0", "Apache-2.0"),
    ("mozilla public license version 2.0", "MPL-2.0"),
    ("bsd 3-clause", "BSD-3-Clause"),
    ("mit license", "MIT"),
)

#: The pyproject `[project].license` shapes PEP 621 permits. A plain string is the modern
#: SPDX-expression form this repo uses; `{text = "..."}` is the legacy table form, still valid
#: and still an identifier; `{file = "LICENSE"}` names a file and asserts no identifier at all.
_PYPROJECT_LICENSE_TEXT_KEY = "text"


def _license_family_from_text(text: str) -> str:
    """Derive the SPDX licence family from a licence document's own title.

    Args:
        text: The full text of a licence file.

    Returns:
        An SPDX family such as ``"AGPL-3.0"`` or ``"MIT"``.

    Raises:
        AssertionError: If the head of the document matches no known licence title. That is
            deliberately loud: a repo that relicensed to something this does not recognise
            must not silently keep passing against the old identifier.
    """
    head = " ".join(" ".join(text.splitlines()[:8]).split()).lower()
    for phrase, family in _LICENSE_TITLES:
        if phrase in head:
            return family
    raise AssertionError(f"could not identify the licence from its own title: {head[:120]!r}")


def _license_family_from_license_file() -> str:
    """Derive the SPDX licence family from the repo's own ``LICENSE``."""
    return _license_family_from_text(LICENSE_FILE.read_text(encoding="utf-8"))


def _current_spdx_ids(family: str) -> set[str]:
    """Return the SPDX identifiers a current document may use for `family`.

    Args:
        family: An SPDX family from :func:`_license_family_from_text`.

    Returns:
        For a disjunctive GNU family, the ``-only``/``-or-later`` pair — the bare family name
        is deliberately excluded, because it is deprecated. For everything else, the family
        name itself.
    """
    if family in _DISJUNCTIVE_FAMILIES:
        return {f"{family}-only", f"{family}-or-later"}
    return {family}


def _tracked_manifests(basename: str) -> list[Path]:
    """Return every GIT-TRACKED file named `basename`, anywhere in the repo.

    ⚠️ **"First-party" means "tracked by git", and enumerating it any other way is what
    broke this module.** It used to walk the filesystem with `rglob` and subtract a
    hand-maintained set of directory names (`node_modules`, `venv`, `dist`, …). That set can
    only ever list the vendored directories somebody has already seen, so the walk collected,
    on a developer machine and in the integration gate alike: **44 manifests from other
    agents' `.claude/worktrees/*` checkouts** (each a full copy of this same repo),
    `reference_repos/open-webui/`, and `backend/venv-eval/lib/python3.12/site-packages/pandas/`
    — the last one slipping through because the exclusion was the literal name `venv` and the
    directory is `venv-eval`. Every one of those is gitignored, so `git ls-files` excludes
    all three classes by construction and needs no list to maintain.

    Args:
        basename: The manifest filename to enumerate, e.g. ``"package.json"``.

    Returns:
        Absolute paths, sorted. Never empty — see the assertion below.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", f"*/{basename}", basename],
        capture_output=True,
        text=True,
        check=True,
    )
    paths = sorted(REPO_ROOT / rel for rel in result.stdout.split("\0") if rel)
    assert paths, (
        f"git ls-files found no tracked {basename} in {REPO_ROOT} — every test below would "
        f"then iterate an empty list and pass having checked nothing"
    )
    return paths


def _first_party_package_jsons() -> list[Path]:
    return _tracked_manifests("package.json")


def _first_party_pyproject_tomls() -> list[Path]:
    return _tracked_manifests("pyproject.toml")


def _pyproject_license_id(path: Path) -> str | None:
    """Return the SPDX identifier a `pyproject.toml` declares, or None.

    PEP 621 allows `[project].license` to be a string (the SPDX-expression form) or a table.
    Reading the raw value and comparing it against a `set` raised
    ``TypeError: unhashable type: 'dict'`` on the first table-form manifest the walk reached
    — a crash where a legible "this file declares no current identifier" was wanted.

    Args:
        path: Path to a `pyproject.toml`.

    Returns:
        The declared identifier, or None when the field is absent or names a **file**
        (``{file = "LICENSE"}``) rather than asserting an identifier.
    """
    with path.open("rb") as handle:
        value: Any = tomllib.load(handle).get("project", {}).get("license")
    if isinstance(value, dict):
        value = value.get(_PYPROJECT_LICENSE_TEXT_KEY)
    if value is None:
        return None
    return str(value)


def _production_dockerfiles() -> dict[str, Path]:
    """Return ``{component: Dockerfile path}`` for every image this repo publishes.

    Read out of ``security-scan.sh``'s ``SCAN_COMPONENT_DOCKERFILE`` table rather than listed
    here, because that table is already the repo's single home for "what do we publish"
    (issue #681) — so a sixth published component comes under this gate the moment it is
    added there, instead of quietly escaping a hand-maintained copy.

    Returns:
        Component name mapped to the absolute path of its Dockerfile.
    """
    text = SECURITY_SCAN_SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"declare -A SCAN_COMPONENT_DOCKERFILE=\((.*?)\n\)", text, flags=re.DOTALL)
    assert block is not None, (
        "SCAN_COMPONENT_DOCKERFILE not found in security-scan.sh — this test derives the "
        "published-image set from it and cannot be allowed to silently check nothing"
    )
    pairs = re.findall(r"\[(\w+)\]=\"([^\"]+)\"", block.group(1))
    return {component: REPO_ROOT / path for component, path in pairs}


def _final_stage(dockerfile_text: str) -> str:
    """Return the portion of a Dockerfile belonging to its LAST build stage.

    A ``LABEL`` in an earlier stage never reaches the published image — only the final
    stage's metadata is committed — so a check that scanned the whole file would pass on a
    declaration no `docker inspect` or SBOM could ever see.

    Args:
        dockerfile_text: The full Dockerfile source.

    Returns:
        Everything from the last ``FROM`` instruction onwards.
    """
    from_positions = [
        match.start()
        for match in re.finditer(r"^\s*FROM\s", dockerfile_text, flags=re.MULTILINE | re.IGNORECASE)
    ]
    if not from_positions:
        return dockerfile_text
    return dockerfile_text[from_positions[-1] :]


def _dockerfile_license_label(dockerfile_text: str) -> str | None:
    """Return the licence the final stage declares via ``LABEL``, or None.

    Joins backslash continuations first, so the usual multi-line ``LABEL a=1 \\ b=2`` block
    is matched as one instruction rather than depending on which physical line the licence
    happens to land on.

    Args:
        dockerfile_text: The full Dockerfile source.

    Returns:
        The label's value, or None when the final stage declares no licence.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", _final_stage(dockerfile_text))
    for line in joined.splitlines():
        if not line.lstrip().upper().startswith("LABEL "):
            continue
        match = re.search(rf'{re.escape(OCI_LICENSE_LABEL)}="([^"]*)"', line)
        if match:
            return match.group(1)
    return None


def _all_declared_license_ids() -> dict[str, str]:
    """Return every first-party licence declaration in the tree, keyed by its source.

    Returns:
        ``{"<relative path>": "<declared SPDX id>"}`` across package.json manifests,
        pyproject.toml manifests, and production Dockerfile labels. A source that declares
        nothing is simply absent — the per-source tests below are what report those.
    """
    declared: dict[str, str] = {}
    for path in _first_party_package_jsons():
        value = json.loads(path.read_text(encoding="utf-8")).get("license")
        if value:
            declared[str(path.relative_to(REPO_ROOT))] = str(value)
    for path in _first_party_pyproject_tomls():
        value = _pyproject_license_id(path)
        if value:
            declared[str(path.relative_to(REPO_ROOT))] = value
    for path in _production_dockerfiles().values():
        value = _dockerfile_license_label(path.read_text(encoding="utf-8"))
        if value:
            declared[str(path.relative_to(REPO_ROOT))] = value
    return declared


def test_license_family_is_derived_from_the_license_file_contents() -> None:
    """Guards the derivation: it must read the file, not return a constant.

    Without this, `_license_family_from_license_file()` could `return "AGPL-3.0"` and every
    other test in this module would still pass while proving nothing about what the repo
    actually ships.
    """
    assert _license_family_from_license_file() == "AGPL-3.0"
    assert _license_family_from_text("                 MIT License\n\nCopyright (c)") == "MIT"
    assert (
        _license_family_from_text("   Apache License\n   Version 2.0, January 2004") == "Apache-2.0"
    )


def test_bare_gnu_family_identifiers_are_rejected_as_deprecated() -> None:
    """A disjunctive family's bare name is not a current SPDX id and must not be accepted.

    This is the rule that fails the real drift #886 found: four production Dockerfiles
    declared `AGPL-3.0`, which SPDX deprecated in favour of the `-only`/`-or-later` pair.
    """
    valid = _current_spdx_ids("AGPL-3.0")
    assert valid == {"AGPL-3.0-only", "AGPL-3.0-or-later"}
    assert "AGPL-3.0" not in valid
    assert _current_spdx_ids("MIT") == {"MIT"}


def test_expected_manifest_set_is_what_the_886_sweep_found() -> None:
    """Guards the guard: if new first-party manifests appear, this sees them too.

    Pins the exact set the 2026-09-09 sweep enumerated, so a manifest silently added
    outside that set cannot be missed by a scanner whose glob quietly matched nothing.

    It is also the assertion that catches over-collection, and it did: when
    `_tracked_manifests` walked the filesystem instead of asking git, this failed with 44
    extra `package.json` files pulled out of other agents' `.claude/worktrees/*` checkouts
    plus `reference_repos/`. An exact `==` is deliberate for exactly that reason — a
    superset check would have called that state clean.
    """
    package_jsons = {str(p.relative_to(REPO_ROOT)) for p in _first_party_package_jsons()}
    pyproject_tomls = {str(p.relative_to(REPO_ROOT)) for p in _first_party_pyproject_tomls()}

    assert package_jsons == {
        "package.json",
        "docs-site/package.json",
        "frontend/package.json",
        "frontend/ffmpeg-wasm-build/test/package.json",
    }
    assert pyproject_tomls == {"pyproject.toml", "backend/pyproject.toml"}


def test_production_dockerfile_set_is_derived_and_non_empty() -> None:
    """Guards the guard: the published-image set must be read, present, and complete.

    An empty or unresolvable table would make the label tests below scan nothing and pass —
    the exact shape `scripts/audit-tests.py` exists to catch.
    """
    dockerfiles = _production_dockerfiles()
    assert set(dockerfiles) == {"backend", "lite", "frontend", "docs", "blackwell"}

    missing = [name for name, path in dockerfiles.items() if not path.is_file()]
    assert not missing, f"SCAN_COMPONENT_DOCKERFILE names files that do not exist: {missing}"


def test_every_production_dockerfile_declares_the_licence_label() -> None:
    """Every published image must carry `org.opencontainers.image.licenses` on its FINAL stage.

    This is the only mechanism that puts the licence into the SBOM for all five components:
    the two nginx images ship no `package.json`, and syft catalogues nothing from a
    `pyproject.toml` (module docstring). A label on a builder stage does not count — it never
    reaches the published image — which is why `_final_stage` narrows the search.
    """
    valid = _current_spdx_ids(_license_family_from_license_file())

    offenders: list[str] = []
    for component, path in sorted(_production_dockerfiles().items()):
        declared = _dockerfile_license_label(path.read_text(encoding="utf-8"))
        if declared not in valid:
            offenders.append(f"{component} ({path.relative_to(REPO_ROOT)}): {declared!r}")

    assert not offenders, (
        f"every production Dockerfile's final stage must declare {OCI_LICENSE_LABEL} as one "
        f"of {sorted(valid)} — offenders: {offenders}"
    )


def test_every_first_party_package_json_declares_the_licence() -> None:
    """A `package.json` missing `license`, or declaring a non-current SPDX id, fails here."""
    valid = _current_spdx_ids(_license_family_from_license_file())

    offenders: list[str] = []
    for path in _first_party_package_jsons():
        license_id = json.loads(path.read_text(encoding="utf-8")).get("license")
        if license_id not in valid:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: license={license_id!r}")

    assert not offenders, (
        f"every first-party package.json must declare a license from {sorted(valid)} — "
        f"offenders: {offenders}"
    )


def test_every_first_party_pyproject_declares_the_licence() -> None:
    """A `pyproject.toml` missing `[project].license`, or a non-current SPDX id, fails here."""
    valid = _current_spdx_ids(_license_family_from_license_file())

    offenders: list[str] = []
    for path in _first_party_pyproject_tomls():
        license_id = _pyproject_license_id(path)
        if license_id not in valid:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: license={license_id!r}")

    assert not offenders, (
        f"every first-party pyproject.toml [project] table must declare a license from "
        f"{sorted(valid)} — offenders: {offenders}"
    )


def test_manifests_and_image_labels_agree_on_one_identifier() -> None:
    """#886's core rule: ONE spelling everywhere, across manifests AND image labels.

    "Two manifests declaring different spellings of the same licence is the drift this issue
    exists to remove" — and the drift that actually existed was between the two planes, not
    within one: manifests said `AGPL-3.0-only`, four Dockerfiles said the deprecated
    `AGPL-3.0`, and the docs image said nothing. Checking each plane against its own
    expectation would never have seen it.
    """
    declared = _all_declared_license_ids()
    expected_sources = (
        len(_first_party_package_jsons())
        + len(_first_party_pyproject_tomls())
        + len(_production_dockerfiles())
    )
    assert len(declared) == expected_sources, (
        f"{expected_sources - len(declared)} first-party source(s) declare no licence at all; "
        f"declared: {sorted(declared)}"
    )

    identifiers = set(declared.values())
    assert len(identifiers) == 1, f"first-party licence identifiers disagree: {declared}"
    assert identifiers <= _current_spdx_ids(_license_family_from_license_file())
