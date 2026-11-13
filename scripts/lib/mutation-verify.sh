#!/usr/bin/env bash
# verify_survivor: re-check ONE claimed survivor against the real source. Sourced by run-mutation-tests.sh,
# which supplies BACKEND, VENV_BIN, MODULE_PATH, MODULE_TESTS and the colour variables.

# Verify ONE claimed survivor by applying its mutation to the real source and running the
# module's tests. This exists because mutmut's own verdict has been wrong here.
#
# `app.auth.lockout.x__check_and_record_attempt_memory__mutmut_13` turns
# `return True, locked_until_dt` into `return False, ...` -- a locked account reporting NOT
# locked -- and mutmut recorded it as SURVIVED. Applied directly to app/auth/lockout.py it
# fails `test_lockout_atomicity.py::test_locked_account_short_circuits_without_writing`
# immediately. So the survivor list has false positives from a THIRD mechanism, after the
# incomplete test list and the stale cache: mutmut's own per-mutant environment and test
# selection. The coverage pre-flight cannot see this one, because it measures coverage
# OUTSIDE mutmut.
#
# A survivor is a claim about the test suite. This turns it into a checked claim. Only
# single-line mutations are handled, and a non-unique or multi-line diff is reported as
# UNVERIFIABLE rather than guessed at.
verify_survivor() {
    # Serialised per module. TWO verifies running against the same file is not a slow
    # verify, it is a corrupt one: each applies a mutation and restores from its own
    # backup, so one process's restore can reinstate the other's mutation -- or leave one
    # behind entirely. Observed here: two mutations applied to lockout.py simultaneously,
    # because a second verify was started while a batch was already looping.
    #
    # The lock is taken on fd 9 for the whole body, so `--verify` in a shell loop is safe
    # and two operators are safe. flock is util-linux, already required elsewhere in this
    # repo's tooling.
    local _lock_file="$OUT_DIR/.verify-$2.lock"
    exec 9>"$_lock_file"
    flock 9
    _verify_survivor_locked "$@"
    local _rc=$?
    exec 9>&-
    return $_rc
}

_verify_survivor_locked() {
    local mutant=$1 module_key=$2
    # A dirty target file is the signature of a verify that DIED between applying its
    # mutation and restoring it. The trap covers INT/TERM/ERR/EXIT, but nothing survives a
    # SIGKILL of the process group -- which is exactly what happened when a batch loop was
    # stopped mid-cycle and left `now < locked_until_dt` mutated to `now <= locked_until_dt`
    # in app/auth/lockout.py. Verifying against an already-mutated file silently reports the
    # wrong verdict, so say so before doing any work. A warning, not a refusal: legitimate
    # uncommitted edits to the module are normal during development.
    if git -C "$REPO_ROOT" diff --quiet -- "backend/${MODULE_PATH[$module_key]}" 2>/dev/null; then
        :
    else
        echo -e "${YELLOW}  ⚠ backend/${MODULE_PATH[$module_key]} has uncommitted changes.${NC}" >&2
        echo -e "${YELLOW}    If a previous --verify was killed, this is its leftover mutation and${NC}" >&2
        echo -e "${YELLOW}    every verdict below is measured against the wrong source. Check with:${NC}" >&2
        echo -e "${YELLOW}      git diff -- backend/${MODULE_PATH[$module_key]}${NC}" >&2
    fi
    local path tests backup rc out
    path="${MODULE_PATH[$module_key]:-}"
    tests="${MODULE_TESTS[$module_key]:-}"
    if [[ -z "$path" ]]; then
        echo "UNVERIFIABLE  unknown module key '$module_key'"; return 2
    fi

    # SAFETY: this transiently mutates the LIVE source file. Two consequences, both real:
    #
    #  * A Ctrl-C, timeout or crash between the write and the restore leaves a mutated
    #    production file behind. Hence the trap: it restores on INT/TERM/ERR/EXIT, not just
    #    on the happy path.
    #  * Do not commit while a verify is running. pre-commit stashes the whole tree, so it
    #    can capture the mutated file mid-cycle -- which has already happened on this branch,
    #    to another agent's in-flight mutation. The batch case is worse than the single case,
    #    so `--verify` says so out loud.
    backup=$(mktemp)
    cp "$BACKEND/$path" "$backup"
    # shellcheck disable=SC2064  # expand $backup and $path NOW, not when the trap fires
    trap "cp '$backup' '$BACKEND/$path' 2>/dev/null; rm -f '$backup'" ERR EXIT
    # INT/TERM must end the run: restoring the file and then carrying on would grade the
    # remaining mutants against the unmutated source (EXIT restores it).
    trap 'exit 130' INT
    trap 'exit 143' TERM

    # The hunk is applied via its CONTEXT lines, not by line number: mutmut prints the
    # diff relative to the mutated FUNCTION, so `@@ -24,7 +24,7 @@` is not a file offset.
    # Context also disambiguates a changed line that occurs more than once -- e.g.
    # `return True, locked_until_dt` appears twice in lockout.py, and a bare
    # search-and-replace either picks the wrong one or gives up.
    # The mutant id carries its function (`...x__check_and_record_attempt_memory__mutmut_13`),
    # which is what disambiguates a context block that appears in more than one place --
    # and it does here: the Redis and in-memory lockout paths are near-duplicates, so 7
    # mutants came back UNVERIFIABLE until the search was scoped to the right function.
    #
    # Class methods carry their class in the id too, joined with U+01C1 (`ǁ`) --
    # `xǁInMemoryStoreǁdelete__mutmut_2` -- and mutmut's own diff for those is printed
    # DEDENTED to the method body's own frame (as if `def delete` started at column 0),
    # not at the method's real column in the file. A top-level function's diff has no such
    # offset (the function already starts at column 0), which is why this went unnoticed
    # until the first `--verify` of a class-method mutant: the context block was searched
    # for at the wrong indentation and matched nowhere. Root-caused and fixed in
    # scripts/mutation-verify-apply.py (issue #459) -- read that file's docstring, not a
    # copy inlined here, so the two never drift.
    mut_func="${mutant##*.}"; mut_func="${mut_func%%__mutmut_*}"; mut_func="${mut_func#x}"
    out=$("$VENV_BIN/mutmut" show "$mutant" 2>/dev/null | MUT_TARGET="$BACKEND/$path" \
        MUT_FUNC="$mut_func" \
        "$VENV_BIN/python" "$REPO_ROOT/scripts/mutation-verify-apply.py")

    case "$out" in
        APPLIED) : ;;
        NODIFF)
            trap - INT TERM ERR EXIT
            cp "$backup" "$BACKEND/$path"; rm -f "$backup"
            echo "UNVERIFIABLE  $mutant (mutmut show produced no usable diff)"; return 2 ;;
        *)
            trap - INT TERM ERR EXIT
            cp "$backup" "$BACKEND/$path"; rm -f "$backup"
            echo "UNVERIFIABLE  $mutant (context block matched ${out#AMBIGUOUS } times)"; return 2 ;;
    esac

    # STAGED, and this is a correctness-preserving optimisation rather than a shortcut: a
    # mutant is KILLED iff ANY selected test fails, so the fast files run first with `-x`
    # and a failure short-circuits the rest. Only a SURVIVED verdict has to pay for the
    # whole list.
    #
    # It matters because `--verify` re-runs the module's ENTIRE list, while mutmut itself
    # runs only the tests covering each mutant. That asymmetry made verification 28-48 s per
    # mutant (lockout's list includes tests/api/test_auth_endpoints.py, a DB + HTTP suite),
    # i.e. hours for one module -- so the tool that exists to make survivors trustworthy was
    # too slow to use on them. KILLED is the common case once tests exist, and it is now the
    # cheap one.
    local fast="" slow=""
    for f in $tests; do
        case "$f" in tests/unit/*) fast="$fast $f" ;; *) slow="$slow $f" ;; esac
    done

    rc=0
    for stage in "$fast" "$slow"; do
        [[ -n "${stage// /}" ]] || continue
        ( cd "$BACKEND" && "$VENV_BIN/python" -m pytest $stage \
            -o addopts= -p no:cacheprovider -p no:testmon -q --no-header -x >/dev/null 2>&1 )
        rc=$?
        [[ $rc -ne 0 ]] && break
    done
    trap - INT TERM ERR EXIT
    cp "$backup" "$BACKEND/$path"
    rm -f "$backup"

    if [[ $rc -eq 0 ]]; then
        echo "SURVIVED      $mutant  (confirmed: the suite does not notice)"
        return 1
    fi
    # Two ways to get here, and the message must not pick one: mutmut's verdict can be
    # wrong, or the suite can have gained a test since the run. --verify always uses the
    # CURRENT suite, which is the question worth answering either way.
    echo "KILLED        $mutant  (the CURRENT suite catches it -- the recorded survivor verdict is stale or wrong)"
    return 0
}
