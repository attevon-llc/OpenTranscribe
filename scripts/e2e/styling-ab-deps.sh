#!/bin/bash
# Sourced by styling-ab.sh. Decides how each A/B tree gets its node_modules.
#
# The shared node_modules (the checkout's) was installed from the checkout's package-lock.json.
# A tree whose lockfile is byte-identical may symlink it (fast path). A tree whose lockfile
# differs gets its own `npm ci` from ITS lockfile: npm ci installs exactly what the lockfile
# records and errors if package.json and the lock disagree, so the result is the tree's real
# dependency set (https://docs.npmjs.com/cli/v10/commands/npm-ci).

# ab_install_mode <tree_frontend_dir> <shared_lock_file>  ->  prints "shared" or "own"
ab_install_mode() {
  if cmp -s "$1/package-lock.json" "$2"; then echo shared; else echo own; fi
}

# ab_provision_node_modules <name> <tree_frontend_dir> <shared_node_modules> <shared_lock> <log_dir>
# Prints which install the tree got and why. npm output goes to <log_dir>/npm-ci-<name>.log.
ab_provision_node_modules() {
  local name="$1" dir="$2" shared="$3" lock="$4" logdir="$5" mode
  mode="$(ab_install_mode "$dir" "$lock")"
  if [ "$mode" = shared ]; then
    ln -sfn "$shared" "$dir/node_modules"
    echo "styling-ab: $name shares the checkout node_modules (package-lock.json identical)"
    return 0
  fi
  echo "styling-ab: $name package-lock.json differs from the checkout's; running npm ci in the tree (log: $logdir/npm-ci-$name.log)"
  mkdir -p "$logdir"
  if ! (cd "$dir" && npm ci --no-audit --no-fund >"$logdir/npm-ci-$name.log" 2>&1); then
    tail -20 "$logdir/npm-ci-$name.log" >&2
    echo "styling-ab: npm ci failed for $name" >&2
    return 3
  fi
  echo "styling-ab: $name has its own node_modules from its own lockfile"
}
