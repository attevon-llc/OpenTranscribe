---
sidebar_position: 14
title: Public demo
description: Run a public, read-only OpenTranscribe demo that visitors cannot change, that starts no background work, and that a published release upgrades on its own.
---

# Public demo

A public demo lets people try OpenTranscribe without installing it. Visitors sign in to a shared
account and browse a library of pre-processed recordings: transcripts, speakers, search and
summaries. They cannot upload, edit, delete or start any processing, and nothing one visitor
does is visible to the next.

This page is for whoever operates a demo. It assumes a host with Docker and a normal installed
deployment (`setup-opentranscribe.sh`).

## What makes it read-only

Four independent layers, each enough to stop a change on its own:

| Layer | What it does |
|---|---|
| The API refuses writes | With `DEMO_MODE=true`, every request that is not `GET`, `HEAD` or `OPTIONS` gets `403` with `{"detail": {"code": "demo_read_only"}}` before it reaches any handler. Only sign-in, token refresh and sign-out are allowed. Live-update connections (WebSockets) are refused too. |
| No background work can be queued | With `DEMO_MODE=true`, the app refuses to publish any background task, so a page view cannot start a scan, a download preparation or anything else. |
| Nothing exists to run work | The demo overlay runs no worker, no scheduler and no Flower. |
| The image cannot transcribe | The demo uses the CPU-only lite image, which has no local speech recognition. |

Every route of the application is checked against the first layer by an automated test, so a
new feature is refused on the demo as soon as it is added.

Chat is turned off on the demo: it would call a language model, which costs money and compute.

## Set up the host

1. Install OpenTranscribe as usual, choosing the lite (CPU-only) deployment.
2. In `.env`, set:

   ```bash
   DEPLOYMENT_MODE=lite
   DEMO_MODE=true
   DEMO_USER_EMAIL=demo@example.com      # shown on your website
   DEMO_USER_PASSWORD=<a password you publish>
   OT_IMAGE_TAG=v0.7.0                    # a released version, never latest
   NGINX_SERVER_NAME=demo.example.org
   ```

3. Put the seed bundle on the host (see below), for example in
   `/opt/opentranscribe-demo-seed/current`.
4. Run the first reset, which seeds the library and opens the site:

   ```bash
   DEMO_SEED_BUNDLE=/opt/opentranscribe-demo-seed/current scripts/demo/demo-reset.sh
   ```

5. Install the timers (both units ship in `scripts/demo/systemd/`; edit the paths first):

   ```bash
   sudo cp scripts/demo/systemd/opentranscribe-demo-*.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now opentranscribe-demo-reset.timer opentranscribe-demo-upgrade.timer
   ```

With `DEMO_MODE=true` in `.env`, `./opentranscribe.sh` adds `docker-compose.demo.yml` to the
compose files by itself. From a repository checkout, `./opentr.sh start prod --build --demo`
does the same (`--demo` implies `--lite`).

### Behind a load balancer or CDN

The app limits requests per visitor address. Behind the bundled nginx this works out of the
box. If something else terminates TLS in front of nginx (an AWS load balancer, CloudFront), add
its address range to `RATE_LIMIT_TRUSTED_PROXIES` in `.env`, for example
`127.0.0.1/32,172.16.0.0/12,10.0.0.0/16`. Never use a range that covers the whole internet:
anyone could then pick their own address by sending an `X-Forwarded-For` header.

`DEMO_RATE_LIMIT_PER_MINUTE` (default 120) is the per-visitor ceiling on every route.

## Nightly reset

`scripts/demo/demo-reset.sh` puts the demo back to its seed every night at 04:00 UTC and after
every upgrade:

1. stops nginx, so the site is closed while it works;
2. deletes all data (`down -v`): database, files, search index and cache together;
3. starts the services without nginx, with one of each worker, and seeds the library;
4. restarts without workers and opens the site again.

If any step fails, the script stops with the site still closed. A half-seeded demo is never
public. The reset takes 10 to 20 minutes, mostly waiting for the search model to load.

## Upgrades

`scripts/demo/demo-upgrade.sh` runs every 15 minutes. It reads the latest published release
from GitHub (or `DEMO_TARGET_TAG`, to run a pre-release), and when it differs from
`OT_IMAGE_TAG` it:

1. runs `./opentranscribe.sh update --version <tag>` and `update-full`;
2. resets the demo from the seed;
3. checks that `/health` answers with the new version.

If the check fails it rolls back with `./opentranscribe.sh update --rollback`, resets again and
exits with an error. It writes the outcome to `demo-status.json` in the install directory; the API reads it (mounted read-only) to show the last upgrade time on the login page. To have `./scripts/release.sh finish` wait for the demo to reach the new tag, set `DEMO_STATUS_CMD` (for example `ssh demo cat /opt/opentranscribe/demo-status.json`) or `DEMO_STATUS_FILE` in the releaser's environment.
`--dry-run` prints every command without running any. Exit codes: `0` upgraded or already
current, `1` upgrade failed and rolled back, `2` bad input, `3` another reset or upgrade is
running, `4` the rollback failed too.

The host stores no credential: it reads public release information and pulls public images.

## Building the seed bundle

The demo never transcribes anything itself. Recordings are processed once, on a machine with a
GPU, and packed into a seed bundle:

```text
manifest.json
media/<item>.mp3
transcripts/<item>.json
```

Each item in `manifest.json` records its source, licence, licence page and attribution. Only
these licences are accepted: CC0-1.0, CC-BY-4.0, CC-BY-SA-4.0, the Public Domain Mark, and works
of the US federal government. Every item must declare that it contains no private individual;
any personal details used to show redaction must be invented and marked `synthetic_pii`.
`demo/seed-catalog.json` lists the candidate sources. Check a bundle before copying it to the
host:

```bash
./opentr.sh shell backend
python -m app.scripts.demo_seed validate --bundle /path/to/bundle
```

## Checks before going public

- Every write route answers `403 demo_read_only` and the WebSocket is refused.
- `python -m app.scripts.assert_inert` reports no task published after browsing every page.
- Database row counts do not change after browsing, apart from sign-in sessions.
- Two visitors behind the proxy are limited separately.
- An upgrade from one pre-release to the next succeeds, and a broken one rolls back.
