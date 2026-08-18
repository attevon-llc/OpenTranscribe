#!/usr/bin/env bash
#
# The venv and the container must install the same versions (issue #492).
#
# `run-integration-tests.sh` — THE pre-merge gate — runs in `backend/venv`, but what
# ships is the image. Those are two installs of the same requirements files, and while
# `requirements.txt` was 61 `>=` floors they drifted **120 packages apart, 18 at a MAJOR
# version** (starlette 0.48 vs 1.6, openai 2.44 vs 3.2, pandas 2.2 vs 3.0). The gate was
# spending its whole runtime validating a program that was not the one shipping.
#
# That is not hypothetical: it is how the NLTK `pathsec` breakage reached production
# green. The venv resolved 3.9.4 and the image 3.10.3; NLTK >=3.10 refuses multiply-linked
# files, so `split_sentences_nltk` raised on EVERY call in the backend and both
# transcription workers — while the host suite passed. No test could have caught it,
# because no test ran against the versions that shipped.
#
# Every requirements file is now exactly pinned, so this compares two things that are
# *supposed* to be identical and reports it when they are not. It replaces a generated
# lock file plus its generator script: with one pinned requirements file per environment,
# installed by both the container and the venv, there is no second artefact to disagree.
#
# Read-only. Reads `pip freeze` from a running container; changes nothing anywhere.

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PIP="${PROJECT_ROOT}/backend/venv/bin/pip"

# pip/setuptools/wheel are the BUILD tooling, not the application. The image upgrades
# them as its first layer and a venv carries whatever `python -m venv` shipped, so they
# differ by construction and say nothing about what the app runs.
IGNORED="pip setuptools wheel"

container="$(docker ps --filter "name=backend" --filter "status=running" --format '{{.Names}}' | head -1)"
if [[ -z "${container}" ]]; then
    echo "SKIP: no running backend container to compare against."
    echo "      Start the stack with './opentr.sh start dev' and re-run."
    exit 0
fi
if [[ ! -x "${VENV_PIP}" ]]; then
    echo "SKIP: no venv at backend/venv — see backend/CLAUDE.md for the bootstrap."
    exit 0
fi

echo "Comparing backend/venv against container '${container}'..."
docker exec "${container}" pip freeze --all 2>/dev/null | sort > /tmp/ot-parity-image.txt
"${VENV_PIP}" freeze --all 2>/dev/null | sort > /tmp/ot-parity-venv.txt

if [[ ! -s /tmp/ot-parity-image.txt ]]; then
    echo "FAIL: could not read 'pip freeze' from ${container}."
    exit 1
fi

IGNORED="${IGNORED}" python3 - "$@" <<'PY'
import os
import re
import sys

def load(path):
    found = {}
    for line in open(path, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line or line.startswith("-") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        found[re.sub(r"[-_.]+", "-", name).lower()] = version.strip()
    return found

ignored = set(os.environ["IGNORED"].split())
image = load("/tmp/ot-parity-image.txt")
venv = load("/tmp/ot-parity-venv.txt")

shared = (set(image) & set(venv)) - ignored
mismatched = sorted(k for k in shared if image[k] != venv[k])
# Packages the IMAGE has and the venv does not are a real gap: the venv cannot exercise
# them. The reverse is expected and fine — requirements-dev.txt (pytest, ruff, mypy,
# mutmut) is venv-only on purpose, and shipping it would bloat the image.
missing = sorted((set(image) - set(venv)) - ignored)

print(f"shared={len(shared)}  mismatched={len(mismatched)}  image-only={len(missing)}")

if not mismatched and not missing:
    print("PASS: the venv installs the same versions the container ships.")
    sys.exit(0)

for name in mismatched:
    print(f"  MISMATCH {name}: image={image[name]}  venv={venv[name]}")
for name in missing:
    print(f"  MISSING  {name}=={image[name]} is in the image but not the venv")

print()
print("The pre-merge gate is not testing what ships. Fix by pinning the package in the")
print("requirements file that owns it and reinstalling both — never by editing one side")
print("to match the other, and never by downgrading a pin (that reintroduces the drift).")
print("  cd backend && venv/bin/pip install -r requirements.txt")
print("  ./opentr.sh rebuild-backend")
sys.exit(1)
PY
