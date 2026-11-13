#!/bin/sh
# The one way every backend image starts the API (Dockerfile.prod / .lite / .blackwell, and any
# compose overlay that sets the backend `command`).
#
# Process count and limits come from the environment, not flags: uvicorn's CLI is built with
# click `auto_envvar_prefix="UVICORN"`, so UVICORN_WORKERS, UVICORN_LIMIT_CONCURRENCY,
# UVICORN_TIMEOUT_WORKER_HEALTHCHECK and UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN are read directly
# (uvicorn settings docs, https://www.uvicorn.org/settings/). UVICORN_WORKERS unset or 1 runs
# one process, which is what a Kubernetes replica wants (FastAPI "Server Workers").
#
# Prometheus multiprocess mode (prometheus_client docs, "Multiprocess Mode"): the directory
# must be set before the application imports prometheus_client and be EMPTY at start, because
# files from a previous run would be aggregated into this one. It is wiped here, in the one
# place that runs before any worker. It is enabled automatically for more than one worker and
# can be forced on or off by setting PROMETHEUS_MULTIPROC_DIR (empty = off). It is deliberately
# not an image ENV: the Celery workers share these images and manage their own directory
# (app/core/worker_metrics.py).
set -eu

workers="${UVICORN_WORKERS:-1}"
if [ -z "${PROMETHEUS_MULTIPROC_DIR+x}" ] && [ "$workers" -gt 1 ]; then
  PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus-multiproc
fi
if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
  rm -rf "$PROMETHEUS_MULTIPROC_DIR"
  mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
  export PROMETHEUS_MULTIPROC_DIR
else
  unset PROMETHEUS_MULTIPROC_DIR
fi

# --proxy-headers makes uvicorn honour X-Forwarded-Proto/-For from the reverse proxy, so
# request.url.scheme is https behind TLS termination and request.client.host is the real
# peer rather than the proxy. Without it the app cannot tell http from https, which
# affects redirect/cookie decisions (issue #284 A0.5).
#
# --forwarded-allow-ips is RATE_LIMIT_TRUSTED_PROXIES, the list app/utils/client_ip.py
# walks: uvicorn rewrites request.client from X-Forwarded-For BEFORE the app sees it, so a
# wider list here is a second resolver the app cannot undo. It used to be every RFC1918
# range, which let any LAN host that reached the API port pick its own client address.
# Unset, uvicorn's own default (loopback) applies (uvicorn settings, "HTTP":
# --forwarded-allow-ips). Never "*".
exec uvicorn app.main:app --host 0.0.0.0 --port 8080 \
  --proxy-headers --forwarded-allow-ips "${RATE_LIMIT_TRUSTED_PROXIES:-127.0.0.1}" \
  "$@"
