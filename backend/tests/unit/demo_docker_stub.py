"""Shell snippet shared by the demo script tests: a stub `docker` that answers stack-target's probes."""

PS_STUB = r"""# `docker ps` is how scripts/lib/stack_target.py resolves the target: answer it from the stub
# (the project asked for, running from $STUB_WORKDIR) and never log it.
if [ "$1" = ps ]; then
  proj=""; for a in "$@"; do case "$a" in label=com.docker.compose.project=*) proj="${a#label=com.docker.compose.project=}";; esac; done
  [ -n "$proj" ] && [ -z "${STUB_NO_STACK:-}" ] && printf '%s-backend-1\t%s\tbackend\trunning\t%s\t\n' "$proj" "$proj" "${STUB_WORKDIR:-$STUB}"
  exit 0
fi
# stack-target's read-only probes (password lookup, inspect) are not part of the contract.
if [ "$1" = inspect ] || { [ "$1" = exec ] && [ "$3" = printenv ]; }; then exit 0; fi
"""
