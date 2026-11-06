#!/usr/bin/env bash
# Sourced helper: where a docker-build-push.sh scan writes its output.
#
# ./security-reports is TRACKED release evidence (security-reports/README.md), so only a
# push-mode run of a release semver may write it. A BUILD_MODE=local or dev-<sha> run used
# to overwrite the committed backend-amd64-* files with a throwaway build's scan; those go
# to the gitignored ./.security-reports-local instead. SCAN_REPORT_DIR is an explicit
# override. Reads BUILD_MODE and VERSION_FULL from the caller.

scan_report_dir() {
    if [ -n "${SCAN_REPORT_DIR:-}" ]; then
        printf '%s\n' "${SCAN_REPORT_DIR}"
    elif [ "${BUILD_MODE:-push}" != "local" ] && [[ "${VERSION_FULL:-}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        printf '%s\n' "./security-reports"
    else
        printf '%s\n' "./.security-reports-local"
    fi
}
