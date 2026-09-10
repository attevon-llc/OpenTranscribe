"""``opentr.sh`` must resolve the compose project the way COMPOSE does — and only that way.

⚠️ **This module previously asserted the opposite, and was wrong.** It claimed opentr.sh had
"three answers and only one was right", on the evidence that every container on the host
carried ``com.docker.compose.project=opentranscribe`` while ``basename "$(pwd)"`` yields
``transcribe-app``, so the port preflight refused to start and the diar-native probe matched
nothing.

Both were behaving **correctly**. The containers were not a dev stack at all: inspecting one
showed ``working_dir=/mnt/nvm/opentranscribe-test-runs/ot-reltest-upgrade-.../rollback`` — a
**leftover release-rehearsal stack**, which by design runs under the stock ``opentranscribe``
name on the standard ports (``scripts/CLAUDE.md``: *"they bind the standard 5173-5180 ports
under the stock opentranscribe-* names ... by design"*). The dev stack was not running.

Teaching the preflight to accept ``opentranscribe`` as "ours" made it wave that stack through,
and ``compose up`` then died on the first hard-coded ``container_name``::

    Conflict. The container name "/opentranscribe-opensearch" is already in use

— the exact part-way-through startup failure (#553) the preflight exists to prevent. The
refusal it replaced was the correct answer, arrived at for the correct reason.

So the rule this module now pins is the narrow one: **one project, resolved as compose
resolves it.** ``ot_stop``'s ``OPENTR_STOP_PROJECT_LABEL``/``_ALT`` pair is deliberately
two-valued because it answers a different question — *clean up anything of ours, including a
leftover rehearsal stack* — and that distinction is the whole point.

What survives from the investigation are two genuinely independent fixes, kept below: the
re-up exemption is decided **per port**, and a **profile-gated** service's port is not
preflight-checked at all.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR.is_file() or shutil.which("bash") is None,
    reason="opentr.sh or bash is not present in this checkout",
)


def _source() -> str:
    return OPENTR.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    text = _source()
    match = re.search(rf"^{re.escape(name)}\(\) \{{(?P<body>.*?)^\}}", text, re.S | re.M)
    assert match, f"{name} has moved or been renamed; re-point this guard"
    return match.group("body")


def _eval(snippet: str, funcs: tuple[str, ...], cwd: str | None = None, env: str = "") -> str:
    extract = "\n".join(f"eval \"$(sed -n '/^{f}() {{/,/^}}/p' {OPENTR})\"" for f in funcs)
    result = subprocess.run(
        ["bash", "-c", textwrap.dedent(f"set -uo pipefail\n{extract}\n{env}{snippet}\n")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=cwd,
    )
    return result.stdout + ("\nSTDERR:" + result.stderr if result.stderr.strip() else "")


def _compose_normalised(name: str) -> str:
    """compose-go's ``NormalizeProjectName``: lowercase, keep ``[a-z0-9_-]``, strip leading ``-_``.

    Reimplemented rather than imported because it lives in Go. It is four lines and is
    pinned against the shell's own answer by
    ``test_the_normalisation_matches_this_modules_oracle``.
    """
    kept = "".join(c for c in name.lower() if c.isascii() and (c.isalnum() or c in "_-"))
    return kept.lstrip("-_")


def test_the_project_is_resolved_exactly_as_compose_resolves_it():
    """⚠️ DERIVED from the checkout's own directory, never hardcoded.

    This asserted ``== ["transcribe-app"]`` — this machine's checkout name — so it could
    only pass in one directory on one host. CI checks out into ``OpenTranscribe`` and it
    failed there, which is how the normalisation bug below was found.
    """
    expected = _compose_normalised(REPO_ROOT.name)
    out = _eval("ot_compose_project", ("ot_compose_project",), cwd=str(REPO_ROOT))
    assert out.split() == [expected], (
        "the resolver does not return compose's own answer (an explicit "
        f"COMPOSE_PROJECT_NAME, else the NORMALISED directory basename): {out!r}"
    )


def test_an_uppercase_checkout_directory_is_normalised_the_way_compose_does(tmp_path: Path):
    """THE bug CI caught, and it is the DEFAULT case, not an exotic one.

    ``git clone`` of this repo produces a directory named ``OpenTranscribe``. Compose
    normalises that to ``opentranscribe``; the resolver echoed the raw basename, so every
    ``--filter label=com.docker.compose.project=$(ot_compose_project)`` matched **nothing**
    — the port preflight would see no container of ours and refuse every re-up in place.

    Invisible on this machine only because its checkout is ``transcribe-app``, already
    normalised. That is precisely why the assertion above must be derived.
    """
    probe = tmp_path / "OpenTranscribe"
    probe.mkdir()
    out = _eval(
        f'cd "{probe}"\not_compose_project',
        ("ot_compose_project",),
    )
    assert out.split() == ["opentranscribe"], (
        "a checkout directory with uppercase letters is not normalised the way compose "
        f"normalises it, so every project-label filter silently matches nothing: {out!r}"
    )


def test_a_directory_name_compose_would_strip_is_stripped(tmp_path: Path):
    """Characters outside ``[a-z0-9_-]`` are DROPPED by compose, not replaced."""
    probe = tmp_path / "My.Repo v2"
    probe.mkdir()
    out = _eval(
        f'cd "{probe}"\not_compose_project',
        ("ot_compose_project",),
    )
    assert out.split() == ["myrepov2"], (
        f"dots/spaces are not dropped the way compose drops them: {out!r}"
    )


def test_the_normalisation_matches_this_modules_oracle(tmp_path: Path):
    """Guard the guard: the Python oracle above and the shell must not drift apart.

    Every assertion in this module that derives an expectation leans on ``_compose_normalised``.
    If it and the shell disagreed, they could agree on being wrong together.
    """
    for raw, expected in [
        ("OpenTranscribe", "opentranscribe"),
        ("transcribe-app", "transcribe-app"),
        ("My.Repo v2", "myrepov2"),
        ("_leading", "leading"),
        ("MiXeD_123-x", "mixed_123-x"),
    ]:
        assert _compose_normalised(raw) == expected, f"oracle wrong for {raw!r}"
        probe = tmp_path / raw
        probe.mkdir()
        out = _eval(
            f'cd "{probe}"\not_compose_project',
            ("ot_compose_project",),
        )
        assert out.split() == [expected], (
            f"shell and oracle disagree for {raw!r}: shell={out!r} oracle={expected!r}"
        )


def test_the_installer_project_name_is_not_silently_accepted():
    """The must-NOT-fire control, and the reason this module was rewritten.

    ``opentranscribe`` is what a release rehearsal names its stack. Accepting it here is
    what let a leftover rehearsal stack be mistaken for this deployment.
    """
    body = _function_body("ot_compose_project")
    assert '"opentranscribe"' not in body and "-opentranscribe}" not in body, (
        "ot_compose_project hardcodes the installer/rehearsal project name. A leftover "
        "rehearsal stack would then be treated as a re-up in place, the port preflight "
        "would wave it through, and `compose up` dies on a container_name conflict."
    )


def test_an_explicit_compose_project_name_wins():
    """A --fresh deployment sets it per-invocation and must be believed."""
    out = _eval(
        "ot_compose_project",
        ("ot_compose_project",),
        cwd=str(REPO_ROOT),
        env="export COMPOSE_PROJECT_NAME=otfresh-probe\n",
    )
    assert out.split() == ["otfresh-probe"], (
        f"an explicitly-set COMPOSE_PROJECT_NAME is not honoured: {out!r}"
    )


def test_the_directory_name_is_derived_not_hardcoded():
    """A checkout in a differently-named directory must keep working."""
    out = _eval(
        "mkdir -p /tmp/ot-projname-probe/some-other-checkout\n"
        "cd /tmp/ot-projname-probe/some-other-checkout\n"
        "ot_compose_project",
        ("ot_compose_project",),
    )
    assert "some-other-checkout" in out.split(), (
        f"the project is not derived from the current directory: {out!r}"
    )


def test_stop_still_covers_both_names_because_it_asks_a_different_question():
    """``stop`` must stay two-valued — that is not the same bug wearing a different hat.

    Cleaning up "anything of ours, including a leftover rehearsal stack" is exactly what
    `stop` is for, and it is why the rehearsal stack that broke a gate run was stoppable
    at all.
    """
    text = _source()
    assert "OPENTR_STOP_PROJECT_LABEL" in text and "OPENTR_STOP_PROJECT_LABEL_ALT" in text, (
        "ot_stop's two-name filters are gone. Unifying them onto ot_compose_project would "
        "make `stop` unable to clean up a stack started under the other name."
    )


# ------------------------------------- the exemption must be PER PORT, not all-or-nothing


def test_the_reup_exemption_is_evaluated_per_port():
    """Recognising our own stack must not waive a port held by something else.

    The exemption was all-or-nothing: if ANY container of ours was running, EVERY bound
    port counted as a re-up in place — including one held by unrelated software. `compose
    up` then aborts part way through on that bind error and strands services in
    ``Created``: the #553 failure, reached through the code written to prevent it.
    """
    body = _function_body("preflight_ports_or_die")
    assert "ot_port_holder_is_ours" in body, "the re-up exemption is not evaluated per port"


def test_a_foreign_holder_is_refused_while_our_own_ports_pass():
    """Drive the real decision against a fake docker, both ways.

    A static check cannot tell "asks per port" from "asks per port and ignores the
    answer", and that distinction is the whole fix.
    """
    out = _eval(
        textwrap.dedent("""
            if ot_port_holder_is_ours 5174; then echo "5174=ours"; else echo "5174=foreign"; fi
            if ot_port_holder_is_ours 9000; then echo "9000=ours"; else echo "9000=foreign"; fi
            if ot_port_holder_is_ours 65533; then echo "unbound=ours"; else echo "unbound=foreign"; fi
        """),
        ("ot_compose_project", "ot_port_holder_is_ours"),
        cwd=str(REPO_ROOT),
        # ⚠️ The "ours" label is DERIVED, not the literal `transcribe-app` this used to
        # hardcode. With the name baked in, the test asserted "the comparison works in a
        # checkout called transcribe-app" — and failed in CI's `OpenTranscribe` checkout
        # for a reason that had nothing to do with per-port evaluation, which is what it
        # is actually about.
        env=textwrap.dedent(f"""
            # 5174 held by a container of THIS project; 9000 by an unrelated one.
            docker() {{
              case "$1" in
                ps)      printf 'ours-backend\\t0.0.0.0:5174->5174/tcp\\nheimdall\\t0.0.0.0:9000->9000/tcp\\n' ;;
                inspect) case "$2" in
                           ours-backend) echo "{_compose_normalised(REPO_ROOT.name)}" ;;
                           *)            echo "some-unrelated-project" ;;
                         esac ;;
              esac
            }}
        """),
    )
    assert "5174=ours" in out, f"our own port reported foreign — every re-up refused: {out!r}"
    assert "9000=foreign" in out, (
        f"a port held by an unrelated container is reported as ours, so the preflight "
        f"waves it through and `compose up` aborts part way: {out!r}"
    )
    assert "unbound=foreign" in out, (
        f"a port nothing published is claimed as ours; a non-Docker listener would then "
        f"be waved through too: {out!r}"
    )


def test_the_holder_check_uses_no_pipes():
    """``docker | grep -q`` would invert a match into a non-match under pipefail.

    Here that inversion reports our OWN container as a foreign holder and refuses every
    re-up. The first draft of this function did exactly that; the repo's existing scanner
    (``test_opentr_docker_probe_sigpipe.py``) caught it.
    """
    body = _function_body("ot_port_holder_is_ours")
    # Precise, not crude: a bare `|` also appears as a `case` alternation
    # (`*":$port->"*|*".$port->"*)`), which is not a pipe. Look for a docker invocation
    # with a reader downstream of it on the same logical line.
    offenders = [
        line.strip()
        for line in body.splitlines()
        # Comments are skipped, or this fires on the header explaining the hazard.
        if not line.strip().startswith("#")
        and "docker " in line
        and re.search(r"docker\b[^|]*\|(?!\|)", line)
    ]
    assert not offenders, (
        "ot_port_holder_is_ours pipes docker output into another reader; under pipefail "
        "an early-exiting reader SIGPIPEs the daemon query and inverts the answer, "
        f"reporting our own container as foreign: {offenders}"
    )

    # Guard the guard. Two rounds of narrowing (a `case` alternation, then this module's
    # own explanatory comment) each risked narrowing it into matching nothing at all,
    # which is indistinguishable from a clean function.
    hazard = "  holder=\"$(docker ps --format '{{.Names}}' | head -1)\""
    assert re.search(r"docker\b[^|]*\|(?!\|)", hazard) and not hazard.strip().startswith("#"), (
        "the detector no longer matches the exact shape it exists to catch"
    )
    benign = '    *":${port}->"*|*".${port}->"*) holder="$name"; break ;;'
    assert "docker " not in benign, "the case-alternation control is no longer benign"


# ----------------------- the preflight may only check ports that WILL actually be bound


def test_a_profile_gated_service_port_is_not_preflight_checked():
    """``step-ca`` is ``profiles: ["pki"]`` and no opentr.sh flag activates that profile.

    ``--with-keycloak-test`` starts Keycloak alone, yet the preflight checked
    ``STEP_CA_PORT`` (default **9000**) because it lives in the same
    ``FRESH_KEYCLOAK_PORT_VARS`` array — so a run could be refused over a port belonging
    to a container that was never going to start. 9000 is heavily contended (MinIO,
    Portainer, and on this host an unrelated container).

    ⚠️ **The entry must STAY in the array for ``--port-offset``.** The two consumers ask
    different questions: "which ports must be renumbered so a --fresh stack cannot
    collide" (all of them, including profile-gated ones, per issue #347) versus "which
    ports will be bound by THIS invocation". Deleting it would fix the preflight by
    breaking the isolation guarantee.
    """
    text = _source()
    assert "OT_PROFILE_GATED_PORT_VARS" in text, (
        "there is no declaration of which port vars belong to profile-gated services"
    )
    assert "STEP_CA_PORT" in text.split("OT_PROFILE_GATED_PORT_VARS", 1)[1][:600], (
        "STEP_CA_PORT is not declared profile-gated"
    )
    keycloak_arr = text.split("FRESH_KEYCLOAK_PORT_VARS=(", 1)[1].split(")", 1)[0]
    assert "STEP_CA_PORT" in keycloak_arr, (
        "STEP_CA_PORT was removed from FRESH_KEYCLOAK_PORT_VARS, which silently drops it "
        "from --port-offset renumbering (issue #347)"
    )


def test_the_preflight_filters_the_gated_vars_out():
    """Declaring the list is not using it."""
    text = _source()
    # rsplit: the FIRST occurrence is the function definition, hundreds of lines above the
    # call site, so a forward split inspects the wrong region and passes vacuously.
    marker = "preflight_ports_or_die "
    assert text.count(marker) >= 2, "expected a definition and a call site"
    pf_region = text.rsplit(marker, 1)[0][-2500:]
    assert "OT_PROFILE_GATED_PORT_VARS" in pf_region, (
        "the profile-gated list is declared but the call site never subtracts it — a list "
        "nothing consults looks identical to one that works"
    )
