# Multi-stage build: Node.js build → nginx:alpine serve
# DOCS_BASE_URL=/docs/ ensures all internal Docusaurus links use /docs/ prefix
# so the site works correctly when proxied by the frontend at /docs/

FROM node:20-alpine AS build
WORKDIR /app

# Install dependencies first (layer caching)
COPY package*.json ./
RUN npm ci

# Copy source and build with /docs/ base URL
COPY . .
ARG DOCS_BASE_URL=/docs/
ENV DOCS_BASE_URL=$DOCS_BASE_URL
# The build context is ./docs-site, so the repo-root VERSION file that
# docusaurus.config.ts normally reads is out of reach. opentr.sh passes it in here;
# if it is absent the homepage simply omits the version badge rather than showing
# a stale one.
ARG OT_VERSION=""
ENV OT_VERSION=$OT_VERSION
RUN npm run build

# Production image: nginx:alpine (~25MB) serving static files.
#
# Pinned to the SAME tag as frontend/Dockerfile.prod, deliberately. This image
# had drifted to nginx:1.27-alpine while the frontend moved to 1.31.2-alpine3.23,
# and the older base was carrying libcrypto3/libssl3 3.3.3-r0 — CVE-2026-31789,
# fixed in 3.3.7-r0. That was the docs image's entire CRITICAL count (2), against
# a frontend scanning 0/0 from the same kind of base.
#
# Two images serving static files behind nginx have no reason to be on different
# bases; when they diverge, only one of them gets looked at. Bump both together.
FROM nginx:1.31.2-alpine3.23

# Pull Alpine security patches at build time so this image does not depend on a
# new nginx base tag being cut every time a CVE lands in libcrypto/libexpat/curl.
# frontend/Dockerfile.prod has done this for a while; docs did not, which is the
# other half of why it was scanning 2 CRITICAL / 33 HIGH against the frontend's
# 0 / 0. Runs before the COPY + chown below so the USER switch at the bottom is
# the only identity change.
#
# curl is also removed. It carries 8 CRITICALs that Alpine has no fix for
# (8.19.0-r0 is the current candidate, and grype offers no fixed version), and
# nothing in this image needs it: nginx depends only on libc/libcrypto/libpcre2/
# libssl/libz, and the HEALTHCHECK below uses busybox wget. An unfixable CVE in a
# package you do not use is removable rather than acceptable.
RUN apk upgrade --no-cache && apk del curl

COPY --from=build /app/build /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Run as non-root. nginx.conf already listens on 8080, so nothing needs a
# privileged port; this image was simply still running as root, which dockle
# flags (CIS-DI-0001) and which the repo's own Dockerfile convention forbids.
# The nginx user exists in the base image — only permissions need setting.
RUN chown -R nginx:nginx /usr/share/nginx/html && \
    chown -R nginx:nginx /var/cache/nginx && \
    mkdir -p /var/log/nginx && \
    chown -R nginx:nginx /var/log/nginx && \
    touch /var/run/nginx.pid && \
    chown nginx:nginx /var/run/nginx.pid

USER nginx

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1:8080/ || exit 1
