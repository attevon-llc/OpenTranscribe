# A Keycloak test IdP that starts in SECONDS instead of ~10 minutes.
#
# The stock image runs `start-dev`, which re-runs Quarkus augmentation on EVERY container
# start: it rebuilds the application index and then walks the built tree calling
# `File.setReadable` on each entry (`JarResultBuildStep`). The output lands in
# /opt/keycloak/lib/quarkus, which is NOT on the data volume, so nothing is ever reused —
# every `./opentr.sh start dev --with-keycloak-test` paid the whole cost again.
#
# Measured on this host, 2026-09-06: ~600 s from container start to "Listening on", of which
# ~584 s was augmentation and ~16 s was Quarkus actually booting. That failed the healthcheck's
# 210 s budget, which failed `up --wait`, which aborted `run-dev-tests.sh --full` before a
# single test ran. Widening the budget to 900 s made the gate pass and still cost ten minutes
# of every full run.
#
# Augmentation is a BUILD step that the stock image defers to run time. So run it here, once,
# and start with `--optimized`, which tells Keycloak the build is already done and to skip
# straight to boot. That is the documented Keycloak production pattern; nothing here is a
# workaround for a bug.
#
# ⚠️ Build-time options must be set BEFORE `kc.sh build` and must MATCH at run time, or
# Keycloak refuses to start `--optimized` (it will not silently re-augment). Those are the
# `KC_*` values below — `KC_DB` and `KC_HEALTH_ENABLED` in particular. Runtime-only settings
# (hostname, admin credentials) stay in the compose file where they can vary per deployment.

FROM quay.io/keycloak/keycloak:26.7.3 AS builder

# Health endpoints move to the management port (9000) and are only served when this is set —
# the compose healthcheck probes 9000, so without it nothing listens and `up --wait` fails on
# a Keycloak that is working fine.
ENV KC_HEALTH_ENABLED=true
# The file-backed H2 database `start-dev` uses. Named explicitly because `--optimized` has no
# dev default to fall back on: this is a disposable test IdP, never a production store.
ENV KC_DB=dev-file

RUN /opt/keycloak/bin/kc.sh build

FROM quay.io/keycloak/keycloak:26.7.3

# The augmented distribution, including the pre-built Quarkus application index.
COPY --from=builder /opt/keycloak/ /opt/keycloak/

ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
