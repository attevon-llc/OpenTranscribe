"""``opentr.sh`` must have ONE answer to "which compose project is this deployment".

It had three, and only one of them was right.

A checkout in ``/mnt/nvm/repos/transcribe-app`` makes compose default the project to
``transcribe-app``. A stack started under the one-liner / ``opentranscribe.sh`` runs as
``opentranscribe``. **Both are legitimate, and a developer machine can have been through
both** — this one has: measured 2026-09-08, every running container carries
``com.docker.compose.project=opentranscribe`` while ``basename "$(pwd)"`` yields
``transcribe-app``.

Three call sites, three behaviours:

1. ``ot_stop`` filters on ``OPENTR_STOP_PROJECT_LABEL`` **and** ``..._ALT`` — both names.
   Correct.
2. ``preflight_ports_or_die`` resolved a single name to decide whether a bound port is
   "ours" (a re-up in place) or someone else's (refuse). It resolved ``transcribe-app``,
   did not recognise the live stack, and **refused to start** — which killed the dev gate
   at overlay bring-up, reporting the developer's own running stack as a foreign process
   squatting on eleven ports.
3. ``diar_native_container_present`` resolved the same single name to decide whether to
   append the native-diarization overlay. Measured on this host: **0 matches** against
   ``transcribe-app``, **1** against ``opentranscribe``. So it drops the overlay and hands
   ``celery-worker`` the silent in-process PyAnnote fallback — precisely the regression
   that probe's own header says it exists to prevent.

⚠️ **The third is worse than the second even though only the second is visible.** A refusal
to start is loud and gets fixed in a minute. A dropped sidecar overlay produces a stack that
runs, transcribes, and reports success while diarizing on the wrong engine.

⚠️ **"Just default to `opentranscribe`" is NOT the fix, and was tried.** That is what the
diar-native probe's header records: hardcoding the name made the probe a no-op on the machine
with the bug. Symmetrically, deriving it from the directory is a no-op on a machine whose
stack was started by the installer. A single name is wrong in one direction or the other; the
resolver has to admit both.
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

# A single-name project resolution: `local project="${COMPOSE_PROJECT_NAME:-$(basename ...)}"`
_SINGLE_NAME = re.compile(r'project="\$\{COMPOSE_PROJECT_NAME:-\$\(basename\s+"\$\(pwd\)"\)\}"')


def _source() -> str:
    return OPENTR.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    text = _source()
    match = re.search(rf"^{re.escape(name)}\(\) \{{(?P<body>.*?)^\}}", text, re.S | re.M)
    assert match, f"{name} has moved or been renamed; re-point this guard"
    return match.group("body")


def test_there_is_one_shared_resolver():
    assert "ot_project_names()" in _source(), (
        "opentr.sh has no single ot_project_names resolver. Three call sites each deciding "
        "for themselves which project is 'ours' is how two of them ended up wrong in "
        "opposite directions."
    )


@pytest.mark.parametrize(
    "func",
    ["preflight_ports_or_die", "diar_native_container_present"],
)
def test_no_call_site_resolves_a_single_project_name(func: str):
    body = _function_body(func)
    assert not _SINGLE_NAME.search(body), (
        f"{func} still derives ONE project name from the checkout directory. On a stack "
        "started as 'opentranscribe' that name matches nothing: the preflight then reports "
        "the developer's own stack as a foreign port squatter and refuses to start, and the "
        "diar-native probe silently drops the sidecar overlay."
    )
    # preflight_ports_or_die reaches it INDIRECTLY, via ot_port_holder_is_ours (the
    # exemption is decided per port). Either route is fine; re-deriving a name is not.
    assert "ot_project_names" in body or "ot_port_holder_is_ours" in body, (
        f"{func} does not go through the shared resolver, so it can drift again"
    )


def test_both_names_are_returned_and_are_overridable():
    """The two documented names must both be there, and neither may be hardcoded-only."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            textwrap.dedent(f"""
                set -uo pipefail
                # Define away everything the prologue needs so we can source just the function.
                eval "$(sed -n '/^ot_project_names() {{/,/^}}/p' {OPENTR})"
                ot_project_names
            """),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(REPO_ROOT),
    )
    names = result.stdout.split()
    assert "opentranscribe" in names, (
        f"the installer/one-liner project name is missing: {result.stdout!r} {result.stderr!r}"
    )
    assert "transcribe-app" in names, (
        "the directory-derived project name is missing — a checkout whose stack compose "
        f"named after the directory becomes invisible: {result.stdout!r}"
    )


def test_the_directory_name_is_derived_not_hardcoded():
    """A checkout in a differently-named directory must keep working.

    This is the constraint the diar-native probe's header calls out explicitly, and it is
    why the fix cannot simply be a two-element literal list.
    """
    result = subprocess.run(
        [
            "bash",
            "-c",
            textwrap.dedent(f"""
                set -uo pipefail
                eval "$(sed -n '/^ot_project_names() {{/,/^}}/p' {OPENTR})"
                mkdir -p /tmp/ot-projname-probe/some-other-checkout
                cd /tmp/ot-projname-probe/some-other-checkout
                ot_project_names
            """),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "some-other-checkout" in result.stdout.split(), (
        "the resolver does not derive the project name from the CURRENT directory, so a "
        f"checkout in a differently-named directory is unmanageable: {result.stdout!r}"
    )


def test_an_explicit_compose_project_name_wins():
    """An operator who sets it means it — and a --fresh deployment sets it per-invocation."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            textwrap.dedent(f"""
                set -uo pipefail
                eval "$(sed -n '/^ot_project_names() {{/,/^}}/p' {OPENTR})"
                COMPOSE_PROJECT_NAME=otfresh-probe ot_project_names
            """),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(REPO_ROOT),
    )
    names = result.stdout.split()
    assert names and names[0] == "otfresh-probe", (
        "an explicitly-set COMPOSE_PROJECT_NAME is not resolved FIRST. A --fresh deployment "
        f"sets it, and must not be told its ports belong to the main stack: {result.stdout!r}"
    )


def test_stop_keeps_covering_both_names():
    """The one site that was already right must not be 'unified' into being wrong."""
    text = _source()
    assert "OPENTR_STOP_PROJECT_LABEL" in text and "OPENTR_STOP_PROJECT_LABEL_ALT" in text, (
        "ot_stop's two-name filters are gone. They are the reason `stop` could always see "
        "the live stack, and test_opentr_stop_container_scoping.py's real-docker guard "
        "depends on both being overridable."
    )


# ------------------------------------- the exemption must be PER PORT, not all-or-nothing


def test_the_reup_exemption_is_evaluated_per_port():
    """Recognising our own stack must not waive a port held by something else.

    The exemption was all-or-nothing: if ANY container of ours was running, EVERY bound
    port was treated as a re-up in place. Measured on this host — the live stack holds
    5173-5183, and an unrelated container (``heimdall``) holds 9000, which the
    keycloak-test overlay's ``STEP_CA_PORT`` defaults to. The coarse exemption waives
    9000 along with the rest, and ``compose up`` then aborts PART WAY THROUGH on the bind
    error, stranding services in ``Created`` — precisely the #553 failure this preflight
    exists to prevent, reached through the code that is supposed to prevent it.

    So the rule is per port: bound by a container of ours -> re-up, fine; bound by
    anything else -> refuse and name it.
    """
    body = _function_body("preflight_ports_or_die")
    assert "ot_port_holder_is_ours" in body, (
        "the re-up exemption is not evaluated per port. A single running container of "
        "ours waives every bound port, including ones held by unrelated software."
    )


def test_a_foreign_holder_is_still_refused_while_our_own_ports_pass():
    """Drive the real decision function against a fake docker, both ways.

    A static check cannot distinguish "asks per port" from "asks per port and ignores the
    answer", and that distinction is the whole fix.
    """
    harness = textwrap.dedent(f"""
        set -uo pipefail
        # Our stack holds 5174; 'heimdall' (an unrelated container) holds 9000.
        docker() {{
          case "$1" in
            ps)      printf 'opentranscribe-backend\t0.0.0.0:5174->5174/tcp\nheimdall\t0.0.0.0:9000->9000/tcp\n' ;;
            inspect) case "$2" in
                       opentranscribe-backend) echo "opentranscribe" ;;
                       *)                      echo "some-unrelated-project" ;;
                     esac ;;
          esac
        }}
        eval "$(sed -n '/^ot_project_names() {{/,/^}}/p' {OPENTR})"
        eval "$(sed -n '/^ot_port_holder_is_ours() {{/,/^}}/p' {OPENTR})"
        if ot_port_holder_is_ours 5174; then echo "5174=ours"; else echo "5174=foreign"; fi
        if ot_port_holder_is_ours 9000; then echo "9000=ours"; else echo "9000=foreign"; fi
    """)
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=60, check=False
    )
    out = result.stdout
    assert "5174=ours" in out, (
        f"a port held by our OWN stack is reported foreign — every re-up would be "
        f"refused: {out!r} {result.stderr!r}"
    )
    assert "9000=foreign" in out, (
        f"a port held by an unrelated container is reported as ours, so the preflight "
        f"waves it through and `compose up` aborts part way: {out!r} {result.stderr!r}"
    )


# ----------------------- the preflight may only check ports that WILL actually be bound


def test_a_profile_gated_service_port_is_not_preflight_checked():
    """``step-ca`` is ``profiles: ["pki"]`` and no opentr.sh flag activates that profile.

    ``--with-keycloak-test`` starts Keycloak only, yet the preflight checked
    ``STEP_CA_PORT`` (default **9000**) because it lives in the same
    ``FRESH_KEYCLOAK_PORT_VARS`` array. 9000 is one of the most contended ports on a
    developer machine — MinIO, Portainer, and on this host an unrelated ``heimdall``
    container — so the run was refused over a port belonging to a container that was
    never going to start. Measured 2026-09-08: this is what killed the dev gate at
    overlay bring-up.

    ⚠️ **The port must stay in the array for ``--port-offset``.** Those two consumers ask
    different questions — "which ports must be renumbered so a --fresh stack cannot
    collide" (all of them, including profile-gated ones, or an operator who does run the
    profile collides silently) versus "which ports will be bound by THIS invocation"
    (only the ones a service being started publishes). Deleting the entry would fix the
    preflight by breaking the isolation guarantee of issue #347.
    """
    text = _source()
    assert "OT_PROFILE_GATED_PORT_VARS" in text, (
        "there is no declaration of which port vars belong to profile-gated services, so "
        "the preflight cannot tell a port that will be bound from one that will not"
    )
    assert "STEP_CA_PORT" in text.split("OT_PROFILE_GATED_PORT_VARS", 1)[1][:600], (
        "STEP_CA_PORT is not declared profile-gated, so the preflight still refuses to "
        "start over port 9000 for a step-ca container that never starts"
    )
    # ...and it must still be offset, or a --fresh stack running the pki profile collides.
    keycloak_arr = text.split("FRESH_KEYCLOAK_PORT_VARS=(", 1)[1].split(")", 1)[0]
    assert "STEP_CA_PORT" in keycloak_arr, (
        "STEP_CA_PORT was removed from FRESH_KEYCLOAK_PORT_VARS. That silently drops it "
        "from --port-offset renumbering (issue #347), so a --fresh stack that does run "
        "the pki profile binds the main stack's port."
    )


def test_the_preflight_filters_the_gated_vars_out():
    """Declaring the list is not using it."""
    text = _source()
    # rsplit, not split: the FIRST occurrence is the function definition, hundreds of
    # lines above the call site, so a forward split inspects the wrong region entirely
    # and the guard passes without looking at anything relevant.
    marker = "preflight_ports_or_die "
    assert text.count(marker) >= 2, (
        "expected a definition and at least one call site; re-point this guard"
    )
    pf_region = text.rsplit(marker, 1)[0][-2500:]
    assert "OT_PROFILE_GATED_PORT_VARS" in pf_region, (
        "the profile-gated list is declared but the preflight call site never subtracts "
        "it — a list nothing consults looks identical to one that works"
    )
