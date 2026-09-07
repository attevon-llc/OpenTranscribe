#!/bin/bash
# Patch helpers that rewrite docker-compose files to isolate a test deployment
# from the production stack. Works on a COPY of the compose file in TEST_ROOT —
# the real repo compose files are never modified.
#
# Requires TEST_PROJECT_NAME and TEST_LABEL to be set (see guardrails.sh).

set -euo pipefail

# Derive prefixes used in volume and container names.
# TEST_PROJECT_NAME is e.g. "ot-reltest-fresh"; the volume prefix must be a
# valid identifier, so we replace '-' with '_'.
cp_prefix_container() { echo "$TEST_PROJECT_NAME"; }
cp_prefix_volume() { echo "${TEST_PROJECT_NAME//-/_}"; }

# cp_apply_name_patch FILE
#   Rewrites all "container_name: opentranscribe-*" to use the test prefix.
cp_apply_name_patch() {
    local file="$1"
    local ctn_prefix
    ctn_prefix="$(cp_prefix_container)"
    # Match only lines like `    container_name: opentranscribe-<svc>` (preserve indent)
    sed -i -E "s|^([[:space:]]*container_name:[[:space:]]*)opentranscribe-|\1${ctn_prefix}-|g" "$file"
}

# cp_apply_volume_patch FILE VOLUME_NAME...
#   Renames top-level volume declarations and references. Pass the unprefixed
#   production volume names (e.g. postgres_data minio_data).
cp_apply_volume_patch() {
    local file="$1"
    shift
    local vol_prefix
    vol_prefix="$(cp_prefix_volume)"
    for name in "$@"; do
        # Rename the top-level volume key (e.g. "  postgres_data:" → "  ot_reltest_fresh_postgres_data:")
        sed -i -E "s|^([[:space:]]*)${name}:([[:space:]]*)$|\1${vol_prefix}_${name}:\2|g" "$file"
        # Rename any references inside services ("      - postgres_data:/var/lib/...")
        sed -i -E "s|(^[[:space:]]*-[[:space:]]*)${name}:|\1${vol_prefix}_${name}:|g" "$file"
        # Rename references of the form "source: postgres_data"
        sed -i -E "s|(^[[:space:]]*source:[[:space:]]*)${name}([[:space:]]*)$|\1${vol_prefix}_${name}\2|g" "$file"
    done
}

# cp_force_pull_policy FILE POLICY
#   Sets pull_policy for all services to the given value (never|always|missing).
#   We use `never` for local 0.4.0 image tests and `always` when exercising a
#   real Docker Hub pull for the v0.3.3 starting point.
cp_force_pull_policy() {
    local file="$1"
    local policy="$2"
    python3 - "$file" "$policy" <<'PY'
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
policy = sys.argv[2]

data = yaml.safe_load(path.read_text())
services = data.get("services", {}) or {}
for name, svc in services.items():
    if not isinstance(svc, dict):
        continue
    # Only set pull_policy on services that reference an image (not a build).
    if "image" in svc:
        svc["pull_policy"] = policy

path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
PY
}

# cp_inject_labels FILE LABEL_KV
#   Ensures every service and top-level volume carries our release-test label
#   so guardrails.cleanup can identify managed resources.
cp_inject_labels() {
    local file="$1"
    local label_kv="$2"  # "com.opentranscribe.release-test=fresh-install"
    python3 - "$file" "$label_kv" <<'PY'
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
label = sys.argv[2]
key, _, value = label.partition("=")

data = yaml.safe_load(path.read_text())

# Services
for name, svc in (data.get("services") or {}).items():
    if not isinstance(svc, dict):
        continue
    labels = svc.get("labels") or {}
    if isinstance(labels, list):
        # Convert list-of-strings form to dict
        labels_dict = {}
        for entry in labels:
            k, _, v = entry.partition("=")
            labels_dict[k] = v
        labels = labels_dict
    labels[key] = value
    svc["labels"] = labels

# Top-level volumes and networks
for section in ("volumes", "networks"):
    for name, obj in (data.get(section) or {}).items():
        if obj is None:
            obj = {}
        if not isinstance(obj, dict):
            continue
        labels = obj.get("labels") or {}
        if isinstance(labels, list):
            labels_dict = {}
            for entry in labels:
                k, _, v = entry.partition("=")
                labels_dict[k] = v
            labels = labels_dict
        labels[key] = value
        obj["labels"] = labels
        data[section][name] = obj

path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
PY
}

# cp_pin_image_tag FILE SERVICE TAG
#   Pins a service's image tag in the copied compose file so upgrade and
#   fresh-install tests can exercise specific versions explicitly.
cp_pin_image_tag() {
    local file="$1"
    local service="$2"
    local tag="$3"
    python3 - "$file" "$service" "$tag" <<'PY'
import sys
from pathlib import Path

import yaml

path, service, tag = sys.argv[1], sys.argv[2], sys.argv[3]
data = yaml.safe_load(Path(path).read_text())
svc = (data.get("services") or {}).get(service)
if svc is None:
    sys.exit(f"service '{service}' not found in {path}")
image = svc.get("image")
if not image:
    sys.exit(f"service '{service}' has no image key in {path}")
repo = image.split(":", 1)[0]
svc["image"] = f"{repo}:{tag}"
Path(path).write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
PY
}

# cp_stage_docs_context SRC_DOCS_DIR DEST_DOCS_DIR
#   Stage docs-site/ into a rehearsal tree WITHOUT its build artifacts.
#
# WHY THIS EXISTS
#
# docker-compose.prod.yml declares `build: context: ./docs-site` for the docs service. The
# staged rehearsal trees force `pull_policy: never`, so that directory has to exist and
# `docker compose config` has to validate — a real user always has it in their checkout, only
# these staged trees need it copied in.
#
# It was copied with a bare `cp -r`. MEASURED on this checkout: docs-site is 1.1 GB across
# 40,164 files, of which node_modules alone is 918 MB / 39,171 files (plus build/ at 64 MB and
# .docusaurus at 1.3 MB). test-upgrade.sh stages it twice per hop and runs
# OT_UPGRADE_SOURCE_MINORS=2 hops, so a rehearsal copied ~4.4 GB / 160,000 files it has no use
# for. Excluding the three artifact directories leaves 45 MB / 287 files.
#
# ⚠️ `docs-site/.dockerignore` does NOT help here and is not a substitute. It governs what
# `docker build` sends to the daemon; `cp` has never heard of it. Both are needed, for two
# different copies of the same tree — hence the exclusion list living here as well.
#
# The excluded directories are exactly the ones the image REBUILDS: `npm ci` recreates
# node_modules and `npm run build` recreates build/ and .docusaurus/. Copying a host-built
# node_modules into a build context is worse than useless — the Dockerfile's `COPY . .` would
# overwrite the layer `npm ci` just produced, with a tree built for whatever platform the host
# happens to be.
#
# rsync when available, tar otherwise: both preserve permissions and neither needs the caller
# to enumerate what to KEEP (a keep-list goes stale silently the first time docs-site grows a
# directory; an exclude-list of build artifacts does not).
CP_DOCS_STAGE_EXCLUDES=(node_modules build .docusaurus)

cp_stage_docs_context() {
    local src="$1" dst="$2"
    [[ -d "$src" ]] || return 0

    rm -rf "$dst"
    mkdir -p "$dst"

    local ex args=()
    if command -v rsync >/dev/null 2>&1; then
        for ex in "${CP_DOCS_STAGE_EXCLUDES[@]}"; do args+=(--exclude "/$ex"); done
        if rsync -a "${args[@]}" "$src/" "$dst/"; then
            return 0
        fi
        # Fall through to tar rather than leaving a half-copied tree behind.
        rm -rf "$dst"
        mkdir -p "$dst"
        args=()
    fi
    for ex in "${CP_DOCS_STAGE_EXCLUDES[@]}"; do args+=(--exclude "./$ex"); done
    tar -C "$src" -cf - "${args[@]}" . | tar -C "$dst" -xf -
}
