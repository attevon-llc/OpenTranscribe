"""Every ``--with-*`` overlay must be isolated by ``--fresh`` (issue #347).

A fresh deployment gets its own compose project, container names, ports and
volumes so it cannot collide with the main stack or survive ``fresh-destroy``.
Issue #347 did that for the aux overlays — but the list is a hand-maintained
dispatch in ``opentr.sh``, and **``--with-llm-test`` was silently missing from
it**. That overlay hard-codes a ``container_name`` AND publishes a loopback
port, so a fresh stack collided with the main one on
``opentranscribe-llm-test-vllm`` / 5195, and because its services were never
recorded in the deployment's ``.aux`` file, ``fresh-destroy`` walked past them
and left a multi-GB vLLM holding a GPU.

Nothing caught it, because nothing enumerated the flags. This does.

Static, over the shell source: starting twelve stacks to find out is not a
test anyone would run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

OPENTR = Path(__file__).resolve().parents[3] / "opentr.sh"

#: Flags deliberately NOT isolated, each with the reason it is exempt. An entry
#: here is a decision, not a backlog item — adding one silently is how the
#: llm-test gap would come back.
EXEMPT: dict[str, str] = {
    "WITH_WATCH_FLAG": (
        "docker-compose.watch.yml binds LIVE host directories and declares no "
        "container_name or ports; opentr.sh warns rather than isolating it, "
        "because there is nothing project-scoped to re-pin."
    ),
    "WITH_BACKUP_FLAG": (
        "docker-compose.backup.yml binds live host directories, same as watch, "
        "and likewise declares no container_name or ports."
    ),
}


@pytest.fixture(scope="module")
def source() -> str:
    assert OPENTR.is_file(), f"opentr.sh not found at {OPENTR}"
    return OPENTR.read_text(encoding="utf-8")


def _with_flags(source: str) -> set[str]:
    """Every ``WITH_*_FLAG`` variable the script tests for."""
    return set(re.findall(r"\bWITH_[A-Z0-9_]+_FLAG\b", source))


def _isolated_flags(source: str) -> set[str]:
    """Flags with a branch in the ``--fresh`` aux dispatch.

    Keyed on the branch appending to ``_aux_files`` / ``_aux_services`` /
    ``_port_vars``, which is what actually causes isolation — not on the flag
    merely being mentioned somewhere in the file.
    """
    isolated: set[str] = set()
    for match in re.finditer(
        r'if \[ -n "\$(WITH_[A-Z0-9_]+_FLAG)" \]; then\n(.*?)\n    fi',
        source,
        re.DOTALL,
    ):
        flag, body = match.group(1), match.group(2)
        if "_aux_files+=" in body or "_aux_services+=" in body or "_port_vars+=" in body:
            isolated.add(flag)
    return isolated


def test_every_with_flag_is_either_isolated_or_explicitly_exempt(source):
    flags = _with_flags(source)
    isolated = _isolated_flags(source)

    unaccounted = flags - isolated - set(EXEMPT)
    assert not unaccounted, (
        f"these --with-* flags are neither isolated by --fresh nor listed as "
        f"exempt: {sorted(unaccounted)}. A fresh stack running one of them will "
        f"collide with the main stack on container names or ports, and "
        f"fresh-destroy will not clean it up."
    )


def test_the_llm_test_overlay_is_isolated(source):
    """The specific regression, named so its absence fails loudly."""
    assert "WITH_LLM_TEST_FLAG" in _isolated_flags(source)
    assert "FRESH_LLM_TEST_SERVICES=(llm-test-vllm llm-test-ollama)" in source
    assert "LLM_TEST_PORT=5195" in source, "the vLLM port must take the fresh offset"
    assert "LLM_TEST_OLLAMA_PORT=5196" in source


def test_the_llm_gpu_id_is_deliberately_not_offset(source):
    """A port offset must never renumber a GPU.

    `LLM_TEST_GPU_DEVICE_ID` names a physical card. Sweeping it into the offset
    machinery alongside the ports would silently move a fresh stack's inference
    onto whichever card happened to be offset-many slots along — on this host,
    someone else's.
    """
    assert "LLM_TEST_GPU_DEVICE_ID=" not in "\n".join(
        re.findall(r"FRESH_[A-Z_]*PORT_VARS=\((.*?)\)", source, re.DOTALL)
    )


def test_the_isolation_scanner_would_notice_an_unisolated_flag():
    """Must-fire control.

    The two tests above are satisfied by a scanner that returns every flag it
    sees. This proves it reads the dispatch body and can report absence — the
    exact failure mode that let llm-test through.
    """
    isolated_shape = (
        'if [ -n "$WITH_EXAMPLE_FLAG" ]; then\n'
        '      _aux_files+=("docker-compose.example.yml")\n'
        "    fi"
    )
    bare_shape = 'if [ -n "$WITH_BARE_FLAG" ]; then\n      echo hello\n    fi'

    assert _isolated_flags(isolated_shape) == {"WITH_EXAMPLE_FLAG"}
    assert _isolated_flags(bare_shape) == set()
    assert _with_flags(bare_shape) == {"WITH_BARE_FLAG"}


@pytest.mark.parametrize("flag", sorted(EXEMPT))
def test_every_exemption_names_a_real_flag_and_carries_a_reason(flag, source):
    """A stale exemption is worse than none: it hides a flag nobody isolated."""
    assert flag in _with_flags(source), f"{flag} is exempt but no longer exists"
    assert len(EXEMPT[flag]) > 40, f"{flag}'s exemption needs a written reason"


def _overlay_files_for_flag(source: str, flag: str) -> set[str]:
    """Compose overlay filenames the flag's OWN dispatch block references.

    Indentation-aware rather than windowed: a fixed line-count window picked
    up a NEIGHBOURING flag's overlay once two dispatch blocks landed close
    together (``WITH_WATCH_FLAG``'s window reached into ``WITH_SMB_TEST_FLAG``'s
    block and reported its port-publishing overlay as WATCH's — a false
    positive that would have made this exact safeguard un-mergeable). The
    opentr.sh dispatch blocks nest with 2-space indents, so the flag's own
    block ends at the first later line matching its own indentation + ``fi``;
    anything nested deeper (the ``if [ -f ... ]`` existence check inside) is
    skipped rather than treated as the close.
    """
    lines = source.splitlines()
    marker = f'if [ -n "${flag}" ]; then'
    files: set[str] = set()
    for i, line in enumerate(lines):
        if marker not in line:
            continue
        indent = line[: len(line) - len(line.lstrip(" "))]
        close = f"{indent}fi"
        j = i + 1
        while j < len(lines) and lines[j] != close:
            j += 1
        block = "\n".join(lines[i:j])
        files.update(re.findall(r"-f (docker-compose\.[\w.-]+\.yml)", block))
    return files


def _isolation_contract_violation(text: str) -> str | None:
    """None if a compose overlay's text declares neither ``ports:`` nor
    ``container_name:`` for any service (i.e. it is safe to exempt from
    ``--fresh`` isolation); otherwise the property that disqualifies it.
    """
    if re.search(r"^\s*ports:\s*$", text, re.MULTILINE):
        return "ports:"
    if re.search(r"^\s*container_name:", text, re.MULTILINE):
        return "container_name:"
    return None


@pytest.mark.parametrize("flag", sorted(EXEMPT))
def test_every_exemption_reason_is_factually_true(flag, source):
    """Guards the exact bug this file exists to catch, one level deeper.

    ``--with-pki``'s exemption *said* "layers onto existing services... rather
    than adding its own" — true of ``container_name``, false of ``ports:``
    (``docker-compose.pki.yml`` publishes ``PKI_HTTPS_PORT``). Nothing checked
    the prose against the file it described, so the wrong exemption sat there
    until read by hand. This reads the actual overlay(s) each exempt flag
    loads and asserts neither property appears — the two an exemption's
    reason is implicitly claiming absent, and the two ``--fresh`` isolation
    (FRESH_*_PORT_VARS / FRESH_*_SERVICES) exists to re-pin.
    """
    repo_root = OPENTR.parent
    overlay_files = _overlay_files_for_flag(source, flag)
    assert overlay_files, (
        f"{flag}'s exemption names no compose overlay in its dispatch block to "
        f"verify the claim against"
    )
    for filename in sorted(overlay_files):
        path = repo_root / filename
        assert path.is_file(), f"{flag} references {filename}, which does not exist"
        violation = _isolation_contract_violation(path.read_text(encoding="utf-8"))
        assert violation is None, (
            f"{filename} (exempt via {flag}) declares '{violation}' — it needs real "
            f"--fresh isolation (FRESH_*_PORT_VARS/_port_vars+= for ports, "
            f"FRESH_*_SERVICES/_aux_services+= for container_name), not an exemption. "
            f"This is exactly the --with-pki regression this test suite exists to catch."
        )


def test_the_exemption_fact_checker_would_notice_a_false_claim():
    """Must-fire control for ``_isolation_contract_violation``.

    Reproduces the actual #5 shape: an exemption whose named overlay DOES
    publish a port (or hard-codes a container_name). Exercises the same
    helper the real test calls, so this cannot pass on logic the real test
    does not use.
    """
    assert _isolation_contract_violation("services:\n  a:\n    ports:\n      - 1:1\n") == "ports:"
    assert (
        _isolation_contract_violation("services:\n  a:\n    container_name: x\n")
        == "container_name:"
    )
    assert _isolation_contract_violation("services:\n  a:\n    image: alpine\n") is None


def test_the_overlay_extractor_finds_the_flag_own_dispatch_reference():
    """Must-fire control for ``_overlay_files_for_flag``.

    Includes the actual regression shape: a NESTED ``if``/``fi`` (the
    existence check every real dispatch block has) between the flag's own
    ``if`` and its close, plus a second flag's block immediately afterward —
    the extractor must stop at its own close and not bleed into the next
    flag's overlay.
    """
    fake_source = (
        'if [ -n "$WITH_FAKE_FLAG" ]; then\n'
        '  if [ -f "docker-compose.fake.yml" ]; then\n'
        "    -f docker-compose.fake.yml\n"
        "  fi\n"
        "fi\n"
        'if [ -n "$WITH_OTHER_FLAG" ]; then\n'
        "  -f docker-compose.other.yml\n"
        "fi"
    )
    assert _overlay_files_for_flag(fake_source, "WITH_FAKE_FLAG") == {"docker-compose.fake.yml"}
    assert _overlay_files_for_flag(fake_source, "WITH_OTHER_FLAG") == {"docker-compose.other.yml"}
