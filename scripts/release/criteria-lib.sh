#!/bin/bash
# The bidirectional criteria contract, in ONE place.
#
# release-criteria.yaml's own header states the rule for adding a stage to it:
# "wire it the same way — bidirectionally, or not at all". Bidirectional means both:
#
#   * a script may not record a criterion the file does not define (a typo'd or
#     invented id must not silently become evidence); and
#   * a script may not finish with a criterion the file defines but it never checked
#     (which is how `docs-build` sat in preflight's list for months, was implemented
#     in verify, and was checked by neither).
#
# 10-preflight.sh and 30-verify.sh each carried their own copy of this machinery. Adding
# a third and fourth copy for `rehearse` and `scan` would recreate precisely the drift the
# file exists to prevent — four independent implementations of "the file and the script
# must agree" is four chances for them to disagree. So it lives here and every consumer
# sources it.
#
# USAGE
#   STAGE_ID=rehearse
#   source "$SCRIPT_DIR/criteria-lib.sh"      # loads SEVERITY[] or exits
#   ...
#   record <id> pass|fail|not-measured [detail] [fix] [waived]
#   ...
#   criteria_assert_all_checked                # the second half of the contract
#   criteria_json                              # criteria[] fragment for --json
#
# EXIT CODES — deliberately inside the shared 0/1/2/3/4 vocabulary, and chosen so that
# adding criteria to a stage does NOT disturb that stage's existing contract:
#   2  misuse — an invalid outcome word, an undefined id, or defined-but-unchecked drift.
#      This is a fault in the pipeline's own wiring, never a statement about the release.
#   3  the criteria file itself is unreadable (a precondition, not a gate result).

CRITERIA_YAML="${CRITERIA_YAML:-scripts/release/release-criteria.yaml}"
# The repo venv has PyYAML; bare python3 usually does too. Either is fine, and an
# unreadable criteria file is a precondition failure, never a silent pass.
CRITERIA_PY=python3
[[ -x backend/venv/bin/python ]] && CRITERIA_PY=backend/venv/bin/python

declare -A SEVERITY=()
declare -A RECORDED=()
RESULTS=()
PASS=0
FAIL=0
WARN=0

while IFS='|' read -r _id _sev; do
    [[ -n "$_id" ]] && SEVERITY["$_id"]="$_sev"
done < <("$CRITERIA_PY" - "$CRITERIA_YAML" "$STAGE_ID" <<'PY'
import sys

try:
    import yaml
except ModuleNotFoundError:
    sys.stderr.write("PyYAML is needed to read the release criteria\n")
    raise SystemExit(1)

path, stage = sys.argv[1], sys.argv[2]
with open(path) as handle:
    doc = yaml.safe_load(handle)
try:
    criteria = doc["stages"][stage]["criteria"]
except (KeyError, TypeError):
    sys.stderr.write(f"{path} defines no criteria for stage '{stage}'\n")
    raise SystemExit(1)
for criterion in criteria:
    print(f"{criterion['id']}|{criterion.get('severity', 'blocking')}")
PY
)

if [[ ${#SEVERITY[@]} -eq 0 ]]; then
    echo -e "\033[0;31mcannot read $CRITERIA_YAML — it defines the severity of every check in '$STAGE_ID'.\033[0m" >&2
    echo "  fix: $CRITERIA_PY -c 'import yaml'   # install PyYAML, or repair the file" >&2
    exit 3
fi

# record ID OUTCOME [DETAIL] [FIX] [waived]
#
# OUTCOME is `pass`, `fail`, or `not-measured`. `not-measured` is neither of the other
# two: the check did not run, so it proves nothing, and against a blocking criterion it
# stops the stage exactly as a failure does. Passing `waived` as the fifth argument
# downgrades one to a warning — for an EXPLICIT operator opt-out only, never for a check
# that merely could not run.
record() {
    local id="$1" outcome="$2" detail="${3:-}" fix="${4:-}" waived="${5:-}"
    local severity="${SEVERITY[$id]:-}"
    local RED='\033[0;31m' GREEN='\033[0;32m' YELLOW='\033[1;33m' NC='\033[0m'

    # The vocabulary is exactly three words. `warn` is NOT one of them: whether a failure
    # warns or blocks is the criteria file's decision, not the caller's, and letting a
    # caller say `warn` is how the two drifted apart before.
    case "$outcome" in
        pass|fail|not-measured) ;;
        *) echo -e "${RED}record: '$outcome' is not a valid outcome for '$id' (pass|fail|not-measured)${NC}" >&2
           exit 2 ;;
    esac

    if [[ -z "$severity" ]]; then
        echo -e "${RED}drift: '$id' is not a criterion of stage '$STAGE_ID' in $CRITERIA_YAML${NC}" >&2
        exit 2
    fi
    if [[ "$waived" == "waived" ]]; then severity=warn; fi
    RECORDED["$id"]=1

    local status
    case "$outcome" in
        pass) status=pass ;;
        *)    [[ "$severity" == "blocking" ]] && status=fail || status=warn ;;
    esac

    case "$outcome:$status" in
        pass:*)
            PASS=$((PASS+1)); echo -e "  ${GREEN}PASS${NC}  $id" >&2 ;;
        not-measured:warn)
            WARN=$((WARN+1))
            echo -e "  ${YELLOW}⊘ NOT MEASURED${NC}  $id  $detail (non-blocking)" >&2 ;;
        not-measured:fail)
            FAIL=$((FAIL+1))
            echo -e "  ${RED}⊘ NOT MEASURED${NC}  $id  $detail — proves nothing, not counted as a pass" >&2
            [[ -n "$fix" ]] && echo -e "        fix: $fix" >&2 ;;
        *:warn)
            WARN=$((WARN+1)); echo -e "  ${YELLOW}WARN${NC}  $id  $detail" >&2 ;;
        *)
            FAIL=$((FAIL+1)); echo -e "  ${RED}FAIL${NC}  $id  $detail" >&2
            [[ -n "$fix" ]] && echo -e "        fix: $fix" >&2 ;;
    esac

    RESULTS+=("$(printf '{"id":"%s","status":"%s","outcome":"%s","severity":"%s","detail":"%s","fix":"%s"}' \
        "$id" "$status" "$outcome" "$severity" "${detail//\"/\'}" "${fix//\"/\'}")")
}

# The other half of the contract: a criterion this stage declares and never checked.
criteria_assert_all_checked() {
    local RED='\033[0;31m' NC='\033[0m'
    local unchecked=() id
    for id in "${!SEVERITY[@]}"; do
        [[ -n "${RECORDED[$id]:-}" ]] || unchecked+=("$id")
    done
    if [[ ${#unchecked[@]} -gt 0 ]]; then
        echo -e "${RED}drift: $CRITERIA_YAML defines these '$STAGE_ID' criteria, which this script never checked:${NC}" >&2
        printf '  %s\n' "${unchecked[@]}" >&2
        echo "  implement them here, or remove them from $CRITERIA_YAML" >&2
        exit 2
    fi
}

# The criteria[] array contents for a --json payload (no surrounding brackets).
criteria_json() {
    local IFS=,
    echo "${RESULTS[*]}"
}
