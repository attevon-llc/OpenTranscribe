#!/usr/bin/env bash
# How a mutation run REPORTS its survivors: file:line for each one, and the end-of-run legend.
# Sourced (by lib/mutation-stack.sh, so by run-mutation-tests.sh too); needs the caller's colour
# variables, and SCRIPT_DIR/BACKEND/MODULE_PATH for survivor_locations, OUT_DIR for the footer.

# file:line for each survivor of module $1 in log $2, from mutmut's own <path>.spans index
# (scripts/lib/mutation_survivor_lines.py). Never fails a run: a mutant it cannot place prints `:?`.
survivor_locations() {
    local key=$1 log=$2
    echo "=== OT-MUTATION-SURVIVOR-LOCATIONS key=$key (mutant<TAB>file:line<TAB>mutated line) ==="
    python3 "$SCRIPT_DIR/lib/mutation_survivor_lines.py" --backend "$BACKEND" --log "$log" \
        --module-path "${MODULE_PATH[$key]}"
}

# survivor_locations, printed AND appended to the module log. Appending alone is how the block
# went unseen: it lands after the OT-MUTATION-SUMMARY END marker (so check_baseline never reads
# it) and never reached the console, so a reader of the run output or of the summary's bare
# `<mutant>: survived` lines saw no file:line (MUTKD, #1373). Never fails a run.
report_survivor_locations() {
    local key=$1 log=$2
    survivor_locations "$key" "$log" | tee -a "$log" || true
}

# The survivor block report_survivor_locations left at the end of a module's log, re-printed for a
# console that did not run the module: the --parallel parent, and the end of a serial run, where it
# would otherwise sit above `mutmut results`' thousands of lines. Says so when the block is missing.
logged_survivor_locations() {
    local key=$1 log=$2 marker="=== OT-MUTATION-SURVIVOR-LOCATIONS key=$1 "
    if grep -qF -- "$marker" "$log" 2>/dev/null; then
        awk -v m="$marker" 'index($0, m) == 1 {f = 1} f' "$log"
    else
        echo "$key: no survivor file:line block in $log (the run did not finish)"
    fi
}

# mutation_run_footer SCRIPT KEY...: the legend, then every module's survivors by file:line, LAST,
# below `mutmut results`' list of every mutant in backend/mutants/ (thousands of lines).
mutation_run_footer() {
    local script=$1 key
    shift
    echo -e "${BLUE}How to read this${NC}"
    echo -e "  ${GREEN}killed${NC}    a test noticed the edit — the line is genuinely checked"
    echo -e "  ${RED}survived${NC}  THE FINDING: the suite runs that line but asserts nothing about it."
    echo -e "            Inspect the diff ($script --show <id>), then either add the missing"
    echo -e "            assertion or conclude the line is dead and delete it."
    echo -e "  ${YELLOW}timeout${NC}   the edit caused a hang; counts as killed, but check it is not a"
    echo -e "            real infinite loop the tests were papering over"
    echo -e "  ${YELLOW}suspicious${NC} much slower than baseline — usually a mutated retry/sleep bound"
    echo
    echo -e "Logs: $OUT_DIR/"
    echo
    echo -e "${BLUE}Survivors of this run, by file:line${NC}"
    for key in "$@"; do
        logged_survivor_locations "$key" "$OUT_DIR/$key.log"
    done
}
