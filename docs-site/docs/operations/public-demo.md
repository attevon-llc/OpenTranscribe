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

Asking new questions in chat is turned off on the demo: it would call a language model, which costs money and compute. Instead the demo user finds a few finished conversations in the chat sidebar, generated once with a real model (see [Pre-generated chat conversations](#pre-generated-chat-conversations)).

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

### Behind Cloudflare on AWS (demo.opentranscribe.app)

The demo runs on a small AWS instance, with DNS and TLS through Cloudflare (proxied, orange
cloud). Visitors reach Cloudflare, Cloudflare reaches nginx on the instance.

1. **DNS.** In Cloudflare, add an `A` (and `AAAA` if the instance has IPv6) record for
   `demo` pointing at the instance's Elastic IP, with the proxy status **Proxied**.
2. **Origin certificate.** In Cloudflare, SSL/TLS, Origin Server, create an Origin CA
   certificate for `demo.opentranscribe.app`. Save the certificate and key on the instance as
   `nginx/ssl/server.crt` and `nginx/ssl/server.key` (key mode 600, never committed). The
   installer picks them up and loads the nginx overlay.
3. **SSL mode.** In Cloudflare, SSL/TLS, Overview, choose **Full (strict)**, so Cloudflare
   validates the origin certificate instead of accepting any. (Flexible would send visitor
   traffic to the instance in clear text.) See
   [Encryption modes](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/) and
   [Origin CA certificates](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/).
4. **Lock the AWS security group** so only Cloudflare can reach the web ports: allow inbound
   TCP 443 (and 80 only if you want the redirect) from
   [Cloudflare's published ranges](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/)
   (`https://www.cloudflare.com/ips-v4` and `/ips-v6`) and nothing else; keep SSH to your own
   address. A security group holds 60 rules by default, so put the ranges in a managed prefix
   list ([AWS: managed prefix lists](https://docs.aws.amazon.com/vpc/latest/userguide/managed-prefix-lists.html))
   and refer to it from the rule. With the group locked, nobody can bypass Cloudflare's rate
   limiting and firewall by hitting the instance directly.
5. **Per-visitor limits.** Cloudflare connects from its own addresses, so without more every
   visitor would share one rate-limit bucket. `docker-compose.demo-cloudflare.yml` (added
   automatically when `DEMO_MODE=true` and the nginx overlay is in use) makes nginx follow
   Cloudflare's guidance in
   [Restoring original visitor IPs](https://developers.cloudflare.com/support/troubleshooting/restoring-visitor-ips/restoring-original-visitor-ips/):
   `set_real_ip_from` for each Cloudflare range and `real_ip_header CF-Connecting-IP`, from
   `nginx/cloudflare-real-ip.conf`
   ([nginx realip module](https://nginx.org/en/docs/http/ngx_http_realip_module.html)). nginx
   uses the header only when the connecting peer is a Cloudflare address; from anyone else it
   is ignored, so it cannot be forged. The backend never reads `CF-Connecting-IP` itself, and
   `RATE_LIMIT_TRUSTED_PROXIES` stays at its default (nginx only). Do not add Cloudflare's
   ranges there.
6. **Keep the ranges current.** The committed file is a pinned snapshot. Refresh it, review
   the diff and commit:

   ```bash
   scripts/demo/refresh-cloudflare-ips.sh           # rewrite from Cloudflare's lists
   scripts/demo/refresh-cloudflare-ips.sh --check   # exit 1 when the snapshot is stale
   ```

   Run the same lists into the security-group prefix list when they change. Cloudflare rarely
   changes them, and announces changes on its IP ranges page.

**Live check.** From any machine, `curl -sI https://demo.opentranscribe.app/health` returns 200
through Cloudflare. Then, from your own address (not Cloudflare):
`curl -sk --resolve demo.opentranscribe.app:443:<instance ip> https://demo.opentranscribe.app/`
must time out (the security group refuses it). To prove the header handling, temporarily open
the group to your address, then send `CF-Connecting-IP: 198.51.100.7` directly: the nginx
access log must show your own address, not `198.51.100.7`. Through Cloudflare, the log must
show the visitor's real address (compare with `curl -s https://www.cloudflare.com/cdn-cgi/trace`
from the same machine). Finally, hit `POST /api/auth/login` with a nonexistent account from two
different networks until one is limited: the other must still get a normal 401.

Other TLS terminators (an AWS load balancer, CloudFront) are not covered by this overlay: add
their subnet to `RATE_LIMIT_TRUSTED_PROXIES` in `.env`, never a range covering the whole
internet, since anyone could then pick their own address with an `X-Forwarded-For` header.

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
conversations/<id>.json      (optional, see below)
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

### Pre-generated chat conversations

The demo cannot answer new questions, so the bundle carries finished conversations. They must
be real model output produced by the application's own chat pipeline over the seeded
recordings; the mock LLM is refused. Generate them once, when you build the bundle, on a
throwaway stack:

1. Seed the bundle's recordings into a `--fresh` stack with `DEMO_MODE=false` and configure a
   real model for the demo user (vLLM on GPU 2, or GPU 1).
2. Write the questions in `demo/conversation-prompts.json` (conversation id, title, the bundle
   item ids it covers, the questions).
3. Run the generator, which asks each question over the chat API and writes the answers, with
   their citations, into the bundle:

   ```bash
   DEMO_USER_PASSWORD=... python3 scripts/demo/generate_demo_conversations.py \
       --bundle /path/to/bundle --base-url http://localhost:<frontend port> \
       --email demo@example.com
   ```

4. Read every answer. Do not edit model text: delete the conversation and regenerate.
5. `python -m app.scripts.demo_seed validate --bundle /path/to/bundle` checks the structure
   (alternating question and answer, provider and model recorded, citations inside the
   conversation's recordings).

The seeder stores each conversation as a finished chat owned by the demo user, with citations
pointing at the seeded recordings, so the visitor can open and read them; sending a message is
refused like every other write.

## Checks before going public

- Every write route answers `403 demo_read_only` and the WebSocket is refused.
- `python -m app.scripts.assert_inert` reports no task published after browsing every page.
- Database row counts do not change after browsing, apart from sign-in sessions.
- Two visitors behind the proxy are limited separately.
- An upgrade from one pre-release to the next succeeds, and a broken one rolls back.
