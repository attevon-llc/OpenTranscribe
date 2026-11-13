"""`scripts/e2e/update-visual-baselines.sh` must keep the properties it exists for.

The visual suite's baselines have rotted twice, and both times the mechanism was
the same: the only discoverable way to refresh them was ``UPDATE_SCREENSHOTS=1``
against whatever stack happened to be running, which on the shared dev stack
bakes the developer's own library content into the reference image. Measured on
``chat_trace``, 2026-09-07 — it reproduced *itself* to 0.0253% on the shared
stack while differing **3.06%** from the committed baseline, entirely because
masked chips carrying real corpus counts changed the flex-wrap point of a row.

The wrapper script closes that. This file keeps it closed. Every test here drives
the **real functions out of the real script** — sourced, with a fake ``docker``
on ``PATH`` where the check is an observation of a running stack — rather than
grepping for the presence of a check. A grep passes against a function that
exists and is never called, which is the exact defect class the repo's own
test-quality auditor was written for.

What is asserted, and why each one is a separate test rather than a comment:

* the shared dev stack is refused three independent ways — by declared offset, by the
  compose project name, and by the project that OBSERVABLY owns the capture ports
* a stack serving a **baked** image (prod / nginx / PKI, which run published
  ``davidamacey/opentranscribe-*`` images) is refused, because a capture there
  photographs the PUBLISHED UI while looking exactly like a successful run
* GPU 0 is refused (it runs unrelated work on this host)
* the checks are *invoked* from ``main``, not merely defined
* ``test_visual_regression.py``'s failure messages name the script — that message
  is what a developer reads at the moment they are about to do this wrong

⚠️ Sourcing the script enables ``set -euo pipefail`` in the caller (it is set at
file scope), so every harness below runs ``set +e`` immediately after sourcing.
Without that, a check *correctly* returning 1 aborts the harness and the test
reports a crash instead of a refusal.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.unit.test_stack_target import FAKE_DOCKER
from tests.unit.test_stack_target import mk

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "e2e" / "update-visual-baselines.sh"
TEST_MODULE = REPO_ROOT / "backend" / "tests" / "e2e" / "test_visual_regression.py"
SCRIPT_REL = "scripts/e2e/update-visual-baselines.sh"


@pytest.fixture
def fake_docker_dir(tmp_path: Path) -> Path:
    """A directory to prepend to PATH holding the SAME fake docker test_stack_target.py uses.

    The script's observation check now goes through scripts/lib/stack_target.py, so its fake is
    the library's: containers are declared as data in ``$FAKE_DOCKER_MODEL`` (JSON).
    """
    binary = tmp_path / "docker"
    binary.write_text(FAKE_DOCKER, encoding="utf-8")
    binary.chmod(0o755)
    (tmp_path / "docker.log").write_text("")
    return tmp_path


def stack_model(tmp_path: Path, containers: list[dict]) -> dict[str, str]:
    model = tmp_path / "model.json"
    model.write_text(json.dumps({"containers": containers}), encoding="utf-8")
    return {"FAKE_DOCKER_MODEL": str(model), "FAKE_DOCKER_LOG": str(tmp_path / "docker.log")}


def run_check(
    call: str,
    *,
    env: dict[str, str] | None = None,
    fake_docker: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source the REAL script and run one of its REAL functions.

    `call` is a bash snippet evaluated after the script is sourced, e.g.
    ``assert_gpu_device_allowed 0``.
    """
    # ⚠️ The `type -t` guard is not defensive noise. `source` of a missing file under
    # `set -uo pipefail` (no `-e`) prints to stderr and CARRIES ON, so every later
    # `<function> ...` is a "command not found" whose output is empty — and a test
    # asserting on empty output then PASSES against a script that does not exist.
    # Measured: without this line, `test_port_matching_is_anchored...` was green in
    # the red-before-green run against `git archive HEAD`. A test that cannot fail is
    # worse than no test.
    script = textwrap.dedent(f"""
        set -uo pipefail
        REPO_ROOT={REPO_ROOT!s}
        source "$REPO_ROOT/scripts/lib/compose-project.sh"
        source "$REPO_ROOT/scripts/lib/stack-target.sh"
        source "$REPO_ROOT/{SCRIPT_REL}"
        set +e   # the script turns on `set -e` at source time
        for fn in derive_surfaces assert_gpu_device_allowed observe_capture_stack; do
            if [[ "$(type -t "$fn")" != "function" ]]; then
                echo "SCRIPT_NOT_SOURCED: $fn is not defined"
                exit 97
            fi
        done
        derive_surfaces
        derive_base_ports
        {call}
        echo "RC=$?"
    """)
    proc_env = dict(os.environ)
    if fake_docker is not None:
        proc_env["PATH"] = f"{fake_docker}{os.pathsep}{proc_env['PATH']}"
    proc_env.update(env or {})
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=proc_env, timeout=120
    )


def rc_of(proc: subprocess.CompletedProcess[str]) -> int:
    """The RC= line the harness echoes, so a crashed harness is not read as a pass."""
    assert "SCRIPT_NOT_SOURCED" not in proc.stdout, (
        f"{SCRIPT_REL} did not source — the check under test never ran, so whatever "
        f"this test would have asserted means nothing.\nstderr:\n{proc.stderr}"
    )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("RC="):
            return int(line.removeprefix("RC="))
    raise AssertionError(
        f"the harness never reached its RC= line — it crashed rather than running the "
        f"check.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# The script has to exist and be runnable at all.
# ---------------------------------------------------------------------------
def test_the_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), (
        f"{SCRIPT_REL} is gone. Without it the only discoverable way to refresh a "
        f"baseline is UPDATE_SCREENSHOTS=1 against the shared dev stack, which is "
        f"how these baselines rotted for 28 frontend commits."
    )
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT_REL} is not executable"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_script_parses() -> None:
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# The values the script DERIVES must match the code that owns them. A copy here
# would be correct today and silently wrong later — the same failure the script
# avoids by deriving them in the first place.
# ---------------------------------------------------------------------------
def test_the_surface_list_is_read_from_the_suites_own_constant() -> None:
    proc = run_check('printf "S=%s\\n" "${ALL_SURFACES[*]}"; true')
    assert rc_of(proc) == 0
    derived = next(
        line.removeprefix("S=").split()
        for line in proc.stdout.splitlines()
        if line.startswith("S=")
    )
    # The oracle evaluates the constant itself (AST), independent of how the formatter lays the
    # list out -- a one-line oracle read an exploded multi-line list as empty.
    tree = ast.parse(TEST_MODULE.read_text(encoding="utf-8"))
    expected = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SURFACES" for t in node.targets)
    )
    assert expected, "the oracle found no SURFACES entries, so it proves nothing"
    assert derived == expected


# ---------------------------------------------------------------------------
# REFUSE THE SHARED DEV STACK — the single most important property in the file.
# ---------------------------------------------------------------------------
def test_port_offset_zero_is_refused() -> None:
    """Offset 0 IS the shared dev stack's ports, by definition."""
    proc = run_check("assert_offset_is_not_the_shared_stack 0")
    assert rc_of(proc) == 1, (
        "the script accepted --port-offset 0, which points the capture at the "
        "shared dev stack. Baselines taken there record the developer's library."
    )
    assert "--fresh" in proc.stderr or "port-offset 100" in proc.stderr, (
        "the refusal must name the right command; a bare 'no' sends the operator "
        "back to the env var this script exists to replace"
    )


def test_an_isolated_offset_is_accepted() -> None:
    """The must-stay-clean control. Without it, a check that refuses EVERYTHING passes."""
    proc = run_check("assert_offset_is_not_the_shared_stack 100")
    assert rc_of(proc) == 0, (
        "the refusal fires on a legitimately isolated stack, so the only way to "
        "use the tool would be to bypass it"
    )


@pytest.mark.parametrize("project", ["opentranscribe", "transcribe-app"])
def test_a_live_compose_project_is_refused(project: str) -> None:
    """The two projects scripts/lib/compose-project.sh calls this repo's live stack."""
    proc = run_check(f"assert_project_is_not_live {project}")
    assert rc_of(proc) == 1


def test_the_fresh_capture_project_is_accepted() -> None:
    proc = run_check("assert_project_is_not_live otfresh-visual")
    assert rc_of(proc) == 0


# ---------------------------------------------------------------------------
# The OBSERVATION checks. The two above are statements of intent; only these can
# catch a stack that is up but is not the one the operator thinks it is.
# ---------------------------------------------------------------------------
def _fresh_stack(project: str = "otfresh-visual", bind_root: str | None = None) -> list[dict]:
    src = bind_root if bind_root is not None else str(REPO_ROOT)
    mounts = [{"Type": "bind", "Source": f"{src}/backend", "Destination": "/app"}]
    fmounts = [{"Type": "bind", "Source": f"{src}/frontend", "Destination": "/app"}]
    health = {"State": {"Status": "running", "Health": {"Status": "healthy"}}}
    return [
        mk(
            f"{project}-backend",
            project,
            "backend",
            "127.0.0.1:5274->8080/tcp",
            inspect={**health, "Mounts": mounts},
        ),
        mk(
            f"{project}-frontend",
            project,
            "frontend",
            "127.0.0.1:5273->5173/tcp",
            inspect={**health, "Mounts": fmounts},
        ),
        mk(f"{project}-postgres", project, "postgres", "127.0.0.1:5276->5432/tcp", inspect=health),
        mk(f"{project}-redis", project, "redis", "", inspect=health),
    ]


def _observe(
    tmp_path: Path, containers: list[dict], fake_docker_dir: Path
) -> subprocess.CompletedProcess[str]:
    # The HTTP readiness probe of `healthy` has no fake here (test_stack_target.py owns it), so
    # it is bounded to 1 s and overridden (recorded on stderr by the library). The override is
    # applied AFTER the check runs, so without the bound it would wait the full 300 s first.
    env = {
        **stack_model(tmp_path, containers),
        "ST_ALLOW": "healthy=http probe is not faked",
        "ST_HEALTH_TIMEOUT": "1",
    }
    # Gitignored fonts are present in a fully set-up checkout and absent from a bare worktree;
    # the check is exercised on its own in test_stack_target.py, so give it a fonts dir here.
    return run_check(
        "FRESH_NAME=visual OPENTR=/bin/true; observe_capture_stack",
        fake_docker=fake_docker_dir,
        env=env,
    )


def test_a_stack_whose_ports_belong_to_the_live_project_is_refused(
    tmp_path: Path, fake_docker_dir: Path
) -> None:
    """The case the declared-intent checks cannot see: the offset ports are somebody else's.

    The LIVE project's containers are listed first, and otfresh-visual publishes nothing, so any
    first-match resolution would report the live stack as the capture stack.
    """
    live = _fresh_stack("transcribe-app")
    proc = _observe(tmp_path, live, fake_docker_dir)
    assert rc_of(proc) != 0, (
        "the capture ports were held by the LIVE stack and observe_capture_stack accepted it"
    )


def test_a_dev_stack_bind_mounting_this_checkout_is_accepted(
    tmp_path: Path, fake_docker_dir: Path
) -> None:
    fonts = REPO_ROOT / "frontend" / "static" / "fonts"
    has_fonts = fonts.is_dir() and any(fonts.iterdir())
    proc = _observe(tmp_path, _fresh_stack(), fake_docker_dir)
    # The fonts are a gitignored, worktree-only asset: a bare worktree must be REFUSED (26),
    # a set-up checkout accepted. Either way nothing else about this stack may fail.
    assert rc_of(proc) == (0 if has_fonts else 26), proc.stderr


def test_a_baked_image_stack_is_refused(tmp_path: Path, fake_docker_dir: Path) -> None:
    """A prod/nginx/PKI shaped stack photographs the PUBLISHED UI while looking like success."""
    stack = _fresh_stack()
    for container in stack:
        container["inspect"]["Mounts"] = []
    proc = _observe(tmp_path, stack, fake_docker_dir)
    assert rc_of(proc) == 23, proc.stderr  # STALE_CODE


def test_a_bind_mount_of_another_checkout_is_refused(tmp_path: Path, fake_docker_dir: Path) -> None:
    proc = _observe(
        tmp_path, _fresh_stack(bind_root="/mnt/nvm/repos/some-other-checkout"), fake_docker_dir
    )
    assert rc_of(proc) == 23, proc.stderr


def test_an_absent_stack_is_refused(tmp_path: Path, fake_docker_dir: Path) -> None:
    proc = _observe(tmp_path, [], fake_docker_dir)
    assert rc_of(proc) == 20, proc.stderr  # NO_PROJECT, never a pass


# ---------------------------------------------------------------------------
# GPU policy
# ---------------------------------------------------------------------------
def test_gpu_zero_is_refused() -> None:
    """GPU 0 runs unrelated work on this host and must never be touched."""
    proc = run_check("assert_gpu_device_allowed 0")
    assert rc_of(proc) == 1


def test_no_gpu_flag_inherits_the_projects_card() -> None:
    """The DEFAULT must not hardcode an index in either direction."""
    proc = run_check('assert_gpu_device_allowed ""')
    assert rc_of(proc) == 0
    assert (
        "--gpu-device"
        not in SCRIPT.read_text(encoding="utf-8").split("start_args=(")[1].split("\n")[0]
    ), "the start command must not pass --gpu-device unconditionally"


def test_a_nonzero_gpu_override_is_allowed() -> None:
    proc = run_check("assert_gpu_device_allowed 2")
    assert rc_of(proc) == 0


# ---------------------------------------------------------------------------
# GUARD THE GUARD: a check that exists and is never called guards nothing.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "function",
    [
        "assert_offset_is_not_the_shared_stack",
        "assert_project_is_not_live",
        "observe_capture_stack",
        "assert_gpu_device_allowed",
    ],
)
def test_every_safety_check_is_actually_invoked(function: str) -> None:
    """Definition is not invocation.

    The tests above prove each function refuses what it should. None of them
    proves the script CALLS it — and a defined-but-uncalled check is
    indistinguishable, in every log the tool produces, from a check that passed.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    definition = f"{function}() {{"
    assert definition in source, f"{function} is gone from {SCRIPT_REL}"
    body = source.split(definition, 1)[1]
    call_sites = [
        line
        for line in body.splitlines()
        if function in line and not line.lstrip().startswith("#") and definition not in line
    ]
    assert call_sites, (
        f"{function} is defined in {SCRIPT_REL} but never called. It would refuse "
        f"nothing while every run still printed a clean bill of health."
    )


# ---------------------------------------------------------------------------
# The two pieces with real logic, exercised for real — no stack needed for either.
# ---------------------------------------------------------------------------
def test_the_change_report_measures_a_known_difference(tmp_path: Path) -> None:
    """ "Show the operator what changed" has to be a MEASUREMENT, not a claim.

    A report that always prints 0.0000% is indistinguishable from a clean
    recapture, and it is the only thing standing between the operator and
    accepting an image nobody looked at. So this builds two PNGs whose
    difference is known by construction — a 20x30 black block in a 100x80 grey
    field, exactly 7.5% of pixels — and asserts the script reports that number.
    The unchanged sibling is the must-stay-clean half: without it, a report that
    called everything CHANGED would pass.
    """
    before, after, work = tmp_path / "before", tmp_path / "after", tmp_path / "work"
    for d in (before, after, work):
        d.mkdir()

    np = pytest.importorskip("numpy")
    image_mod = pytest.importorskip("PIL.Image")

    grey = np.full((100, 80, 3), 200, dtype=np.uint8)
    changed = grey.copy()
    changed[10:30, 10:40] = 10
    image_mod.fromarray(grey).save(before / "gallery-light.png")
    image_mod.fromarray(changed).save(after / "gallery-light.png")
    image_mod.fromarray(grey).save(before / "gallery-dark.png")
    image_mod.fromarray(grey).save(after / "gallery-dark.png")

    proc = run_check(
        f'DRY_RUN=false; BEFORE_DIR="{before}"; SCREENSHOT_DIR="{after}"; '
        f'WORKDIR="{work}"; SELECTED_SURFACES=(gallery); report_changes'
    )
    assert rc_of(proc) == 0, proc.stderr

    report = (work / "report.tsv").read_text(encoding="utf-8")
    assert "gallery-light\tCHANGED\t7.5000%" in report, (
        f"the change report did not measure the constructed 7.5% difference:\n{report}"
    )
    assert "gallery-dark\tidentical\t0.0000%" in report, (
        f"an unchanged surface was not reported as identical:\n{report}"
    )
    assert (work / "gallery-light.diff.png").is_file(), (
        "no diff image was written, so there is nothing for the operator to look at "
        "and the confirmation prompt is asking them to approve a number"
    )
    assert (work / "gallery-light.old.png").is_file()
    assert (work / "gallery-light.new.png").is_file()


def test_the_provenance_sidecar_is_valid_json_carrying_the_commit(tmp_path: Path) -> None:
    """Runs the real writer, then PARSES what it produced.

    The static sibling below can only see that the field names appear in the
    heredoc. A missing quote or an unescaped `"` in `--reason` produces a file
    that still contains every field name and that nothing can read.
    """
    import json
    import shlex

    shots, work, out = tmp_path / "shots", tmp_path / "work", tmp_path / "out"
    for d in (shots, work, out):
        d.mkdir()
    (shots / "chat_trace-light.png").write_bytes(b"")
    (work / "report.tsv").write_text("chat_trace-light\tCHANGED\t3.0600%\n", encoding="utf-8")

    # ⚠️ shlex.quote, NOT repr(). Python's repr escapes a backslash for PYTHON
    # (`\` -> `\\`), while a bash single-quoted string takes every character
    # literally — so repr() hands the script two backslashes and the sidecar
    # correctly escapes both, which reads exactly like a double-escaping bug in
    # the script. Caught by this test on its first run; the script was fine.
    reason = 'rows regrouped; a "quoted" phrase and a \\backslash'
    proc = run_check(
        f'SCREENSHOT_DIR="{shots}"; PROVENANCE_DIR="{out}"; WORKDIR="{work}"; '
        f"SELECTED_SURFACES=(chat_trace); REASON={shlex.quote(reason)}; FRESH_NAME=visual; "
        f'PORT_OFFSET=100; GPU_DEVICE=""; resolve_git_provenance; record_provenance'
    )
    assert rc_of(proc) == 0, proc.stderr

    sidecar = json.loads((out / "chat_trace-light.json").read_text(encoding="utf-8"))
    assert sidecar["reason"] == reason, "the reason was mangled by JSON escaping"
    assert len(sidecar["git_sha"]) == 40, (
        "the sidecar does not carry a full HEAD sha, so a baseline cannot be "
        "attributed to the code that produced it"
    )
    assert isinstance(sidecar["git_dirty"], bool), (
        "git_dirty must be a JSON boolean — a string 'false' is truthy to every "
        "consumer that reads it"
    )
    assert sidecar["change_from_previous"]["differing_pixels"] == "3.0600%"


def test_provenance_is_recorded_outside_the_baseline_directory() -> None:
    """A text file inside __screenshots__/ could clear the release staleness gate.

    `scripts/release/30-verify.sh`'s `visual-baselines-fresh` criterion reads
    `git log -1 -- backend/tests/e2e/__screenshots__`, so any committed file in
    that directory counts as "the baselines were refreshed".
    """
    source = SCRIPT.read_text(encoding="utf-8")
    provenance_line = next(
        line for line in source.splitlines() if line.startswith("PROVENANCE_DIR=")
    )
    assert "__screenshots__" not in provenance_line, (
        "the provenance sidecars were moved into the baseline directory, where a "
        "commit touching only them would silently clear visual-baselines-fresh"
    )


def test_the_sidecar_records_the_commit_and_whether_the_tree_was_dirty() -> None:
    """A reference image is a claim about the UI AT A COMMIT.

    Without the sha, nobody can tell whether a baseline predates a feature or
    postdates it — which is exactly the confusion that let chat_trace go stale
    unnoticed. Without the dirty flag, a baseline captured from uncommitted work
    is indistinguishable from one anybody else can regenerate.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    body = source.split("record_provenance() {", 1)[1].split("\n}", 1)[0]
    for field in ('"git_sha"', '"git_dirty"', '"reason"'):
        assert field in body, f"the provenance sidecar no longer records {field}"
    assert "status --porcelain" in source, (
        "nothing computes whether the tree was dirty, so git_dirty would be a "
        "constant — a field that cannot be false records nothing"
    )


# ---------------------------------------------------------------------------
# The message a developer reads at the moment they are about to do this wrong.
# ---------------------------------------------------------------------------
def test_the_suites_failure_messages_name_the_script() -> None:
    """A failing baseline must point at the tool, not at the raw env var.

    This is the whole delivery mechanism: `pytest.fail`'s text is what a
    developer sees the instant they decide how to refresh a baseline. When it
    said `UPDATE_SCREENSHOTS=1 pytest ...`, that is what they ran — against
    whatever stack was up.
    """
    source = TEST_MODULE.read_text(encoding="utf-8")

    # The two pytest.fail bodies in _compare_or_write, plus the module docstring.
    compare = source.split("def _compare_or_write", 1)[1].split("\n# ---", 1)[0]
    assert compare.count(SCRIPT_REL) >= 2, (
        f"_compare_or_write's failure messages no longer both name {SCRIPT_REL}. "
        f"A missing baseline and a changed baseline are the two moments a "
        f"developer decides how to regenerate one."
    )

    docstring = source.split('"""', 2)[1]
    assert SCRIPT_REL in docstring, (
        f"the module docstring no longer names {SCRIPT_REL}, so the documented "
        f"refresh path is back to being the raw env var"
    )
    assert "UPDATE_SCREENSHOTS=1 pytest" not in docstring, (
        "the docstring documents the bare env-var invocation as the refresh path "
        "again — that is the wrong path, and it is the only one anybody reads"
    )


def test_the_isolated_stack_skip_message_names_the_script() -> None:
    """The skip a developer hits on the shared stack is the same decision point."""
    source = TEST_MODULE.read_text(encoding="utf-8")
    guard = source.split("def _skip_unless_isolated_stack", 1)[1].split("\ndef ", 1)[0]
    assert SCRIPT_REL in guard, (
        f"_skip_unless_isolated_stack's message no longer names {SCRIPT_REL}. It is "
        f"read precisely when someone is about to hand-assemble the incantation."
    )


def test_the_change_report_runs_without_a_backend_venv() -> None:
    """CI has no `backend/venv`, and the change report must still work there.

    `report_changes` is pure image maths (numpy + PIL + `_visual_diff`) — none of the
    venv-only stack. It nonetheless invoked the hardcoded `backend/venv/bin/python`, so in
    CI it died with a bare **rc 127, "No such file or directory"**: a precondition failure
    wearing the costume of a broken report. `test_the_change_report_measures_a_known_
    difference` above cannot catch it, because on a developer machine the venv exists.

    Asserted by resolving the interpreter with the venv path forced absent, which is the
    only way to reach CI's condition from here.
    """
    proc = run_check(
        'VENV_PY="/nonexistent/venv/bin/python"; '
        'if [[ -x "$VENV_PY" ]]; then P="$VENV_PY"; '
        'else P="$(command -v python3 2>/dev/null || true)"; fi; '
        'printf "RESOLVED=%s\\n" "$P"; [[ -x "$P" ]]'
    )
    assert "RESOLVED=" in proc.stdout, proc.stdout + proc.stderr
    resolved = proc.stdout.split("RESOLVED=", 1)[1].splitlines()[0]
    assert resolved and Path(resolved).exists(), (
        "with no venv the report has no interpreter to fall back to, so it dies with a "
        f"bare rc 127 in CI: resolved={resolved!r}"
    )


def test_the_report_interpreter_is_not_the_hardcoded_venv_path() -> None:
    """Guard the fix itself: `report_changes` must not go back to `$VENV_PY` directly.

    The full-capture path (pytest/Playwright/`requests`) genuinely REQUIRES the venv and
    `check_preconditions` still enforces that — so this is specifically about the
    pure-maths half, and reverting it would silently restore the CI failure.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("report_changes() {")
    body = text[start : text.index("\n}", start)]
    assert '"$VENV_PY"' not in body, (
        "report_changes invokes $VENV_PY again; in CI that path does not exist and the "
        "function dies with rc 127 before measuring anything"
    )
    assert '"$REPORT_PY"' in body, "report_changes no longer uses the resolved interpreter"


def test_the_full_capture_still_requires_the_real_venv() -> None:
    """MUST-STAY-CLEAN. The fallback must not weaken the precondition it sits beside.

    If `check_preconditions` ever accepted a bare `python3`, a real capture would get past
    the gate and then fail deep inside pytest with no Playwright — much worse than the
    named refusal it gives today.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("check_preconditions() {")
    body = text[start : text.index("\n}", start)]
    assert '-x "$VENV_PY"' in body, (
        "check_preconditions no longer requires the real venv, so a capture run can start "
        "without pytest/Playwright and fail much later for an unrelated-looking reason"
    )
