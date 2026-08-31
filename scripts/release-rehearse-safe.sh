#!/bin/bash
# Autonomous-safe entry point for the rehearsal-only portion of the release
# pipeline. Exists so an agent (or an operator) can iterate on
# preflight/verify/test/build/scan/rehearse without a permission prompt on
# every invocation, while making it STRUCTURALLY impossible for this specific
# entry point to reach tag/publish/promote/finish -- those stages are not
# accepted as arguments here at all, never mind executed.
#
# scripts/release.sh's own EXTERNAL_STAGES gate (tag/publish/promote/finish
# require --yes, checked in code) is the real safety net and is untouched by
# this wrapper. This script is a second, independent layer: it cannot ask for
# --yes because it never accepts an external stage in the first place.
#
# Usage: ./scripts/release-rehearse-safe.sh <version>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VERSION="${1:?usage: release-rehearse-safe.sh vX.Y.Z}"

SAFE_STAGES="preflight,verify,test,build,scan,rehearse"

exec "$SCRIPT_DIR/release.sh" run "$VERSION" --only "$SAFE_STAGES" --yes
