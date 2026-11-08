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

## Runbook order

A separate team can deploy and run the demo from this page alone. Work through it in this
order; each step names the section that has the detail.

1. [Prerequisites](#prerequisites)
2. [Instance and security group](#why-a-separate-instance) and [sizing](#instance-size-and-cost)
3. [Cloudflare DNS, SSL mode and Origin CA](#behind-cloudflare-on-aws-demoopentranscribeapp)
4. [Install with the demo and Cloudflare overlays](#set-up-the-host)
5. [Place the seed bundle and run the first reset](#building-the-seed-bundle)
6. [The nightly reset timer](#nightly-reset)
7. [Auto-upgrade on release and the status file](#upgrades)
8. [Billing and disk alarms](#billing-and-disk-alarms)
9. [Verification checklist](#verification-checklist)
10. [Rollback](#rollback)
11. [Troubleshooting](#troubleshooting)

## Prerequisites

- An AWS account you can create an EC2 instance, an Elastic IP, a managed prefix list, a budget
  and CloudWatch alarms in.
- A domain on Cloudflare (the demo uses `demo.opentranscribe.app`) and permission to add a DNS
  record and an Origin CA certificate.
- An email address (or SNS topic) for alarms, and someone who reads it.
- A seed bundle built by the project (see [Building the seed bundle](#building-the-seed-bundle))
  and its path on the host. The demo host never builds one.
- Outbound internet from the host to Docker Hub and `api.github.com` (release lookups and image
  pulls). The host stores no credential.
- A decision about which release the demo runs. By default it follows the latest published
  release; set `DEMO_TARGET_TAG` to pin a pre-release.

## Why a separate instance

Run the demo on its own low-cost instance, never on staging or production, for three reasons:

- **Passwordless public sign-in.** "Try the demo" signs anyone on the internet in without a
  password (`POST /api/auth/demo/session`). That must never share a database, object store or
  network with real users' data.
- **Nightly reset.** Every night at 04:00 UTC the reset deletes all data (`down -v`) and
  reseeds it, and every upgrade does the same. On a shared host that would wipe real data.
- **Isolation.** A public, anonymous audience is a different abuse and capacity profile.
  Its own security group (Cloudflare only), its own budget and its own alarms mean a problem on
  the demo cannot take down, or run up the bill of, anything else.

The demo is inert by design: no worker, no scheduler, no GPU and no language model, so it needs
a small CPU instance, and chat is limited to the canned conversations in the bundle.

## Instance size and cost

Target an AWS Graviton (arm64) `t4g.large` (2 vCPU, 8 GiB). The lite image is multi-arch
(`./scripts/docker-build-push.sh list-platforms` prints `lite  cpu  linux/amd64,linux/arm64`),
so it runs natively on Graviton. Move to `t4g.xlarge` (4 vCPU, 16 GiB) only if OpenSearch (the
largest consumer) runs short of memory: watch `docker stats` and `free -m` after a reset and
under a few visitors. Resize **vertically** (stop, change instance type, start); do not add
autoscaling, since the demo is one stateful host that resets itself.

On-demand Linux prices in us-east-1, read on 2026-11-08 (a mirror of the AWS price list, as the
EC2 pricing page is JavaScript-rendered; confirm on the
[EC2 on-demand pricing page](https://aws.amazon.com/ec2/pricing/on-demand/) before you commit):

| Instance | vCPU / memory | Per hour | About per month (730 h) |
|---|---|---|---|
| `t4g.large` (Graviton2) | 2 / 8 GiB | $0.0672 | $49 |
| `t3.large` (Intel) | 2 / 8 GiB | $0.0832 | $61 |
| `t4g.xlarge` (Graviton2) | 4 / 16 GiB | $0.1344 | $98 |
| `t3.xlarge` (Intel) | 4 / 16 GiB | $0.1664 | $121 |

The t4g is 19 to 20% cheaper than the matching t3. AWS's own claim, on the
[Graviton page](https://aws.amazon.com/ec2/graviton/), is that Graviton instances cost "up to
20% less than comparable x86-based" instances, and it describes Graviton as "the best price
performance for a broad range of workloads" (no benchmark figure is given there).

Budget about **$50 to $100 per month** for compute, plus roughly $4 for a 50 GiB `gp3` volume
and $3.65 for the public IPv4 address, so about $57 on `t4g.large` and about $106 on
`t4g.xlarge`. Cloudflare's free plan is enough. Caching and limits at Cloudflare keep traffic
off the instance: cache the static frontend assets (the `/assets/` path), never cache `/api/`,
and add a Cloudflare rate-limiting rule on `/api/auth/demo/session` in addition to the per-visitor
limits nginx enforces (see [Cloudflare per-visitor limits](#behind-cloudflare-on-aws-demoopentranscribeapp)).
T4g instances launch in "unlimited" CPU-credit mode, so sustained bursts bill surplus credits:
alarm on `CPUSurplusCreditsCharged` too, or choose standard mode.

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

### What goes in the bundle

`demo/seed-catalog.json` lists the candidate sources by collection. Each item in a bundle names
its `collection`, one of: News, Government meetings, Business & finance, Training & education,
Science & briefings, Privacy demo. The seeder creates each collection for the demo user and files
the item in it; `demo_seed validate` refuses an item whose collection is missing or unknown.

| Collection | Sources | Licence |
|---|---|---|
| News | Voice of America (several languages) | US-GOV-PD |
| Government meetings | FCC open commission meetings, NTSB board meetings, FDA advisory committee meetings | US-GOV-PD |
| Business & finance | Federal Reserve FOMC press conferences, SEC open meetings | US-GOV-PD |
| Training & education | NASA education, FEMA and OSHA training videos, LibriVox readings | US-GOV-PD, PDM-1.0 |
| Science & briefings | NASA, NOAA / National Weather Service | US-GOV-PD |
| Privacy demo | The scripted clip with invented personal details | CC0-1.0 |

The catalogue records, for every source, its licence, licence page, attribution, why it is
allowed, and exclusion rules (for example, public-comment segments by private citizens are always
cut, and third-party material a page marks as such is excluded). Two licence pages could not be
fetched when the catalogue was written (FCC, OSHA); they are marked `license_verified: false`.
Re-read every licence page and set `license_checked_on` when you build the bundle. LibriVox is
held back until the project owner decides whether volunteer readers count as private individuals.
The owner approves the final item list.

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

## Billing and disk alarms

Create both before the demo is public. Alarm mail goes to a monitored address.

**Monthly cost budget** ([AWS Budgets, Creating a budget](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html)):

1. Open the Billing and Cost Management console, then **Budgets**, **Create budget**.
2. Choose the **Customize (advanced)** template and **Cost budget**. Next.
3. Name it `opentranscribe-demo`, period **Monthly**, **Recurring**, **Fixed** amount `$100`
   (or $120 for `t4g.xlarge`). Under filters, scope it to the demo's account, or to a cost
   allocation tag `project=opentranscribe-demo` set on the instance and volume.
4. Add alert thresholds: **Actual** at 80% and 100%, and **Forecasted** at 100%, each with the
   alarm email as recipient (see
   [Adding email recipients](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-email-recipients.html)).
5. Create budget. Budgets updates about three times a day, so it is a safety net, not a meter.

**Disk-full alarm.** EC2 does not publish disk use; the CloudWatch agent does
([Collect metrics with the CloudWatch agent](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Install-CloudWatch-Agent.html)).

1. Attach an instance role with the `CloudWatchAgentServerPolicy` managed policy.
2. Install the agent and a config that collects `disk` `used_percent` for `/` and the Docker
   data path (it publishes `disk_used_percent` in the `CWAgent` namespace; see
   [Metrics collected by the CloudWatch agent](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/metrics-collected-by-CloudWatch-agent.html)).
   Start it with `amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c file:<config>`.
3. In CloudWatch, **Alarms**, **Create alarm**, select metric `CWAgent` > `disk_used_percent`
   for the instance and path `/`; statistic Maximum, period 5 minutes; threshold greater than
   or equal to `80`, 2 of 2 datapoints. Notification: an SNS topic that emails the alarm
   address ([Alarm that sends email](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html)).
4. Add a second alarm at `90` for escalation, and one on the built-in `StatusCheckFailed`
   metric, which needs no agent.

The nightly reset empties every store, so a rising disk almost always means old Docker images
from upgrades: `docker image prune -f` (never `-a` on a shared daemon).

## Verification checklist

Run every item once before the demo is public, and again after the first upgrade.

- [ ] `curl -sI https://demo.opentranscribe.app/health` answers 200 through Cloudflare.
- [ ] Direct to the instance, bypassing Cloudflare, times out (see the live check above).
- [ ] The login page shows "Try the demo", the version and the last upgrade time; clicking it
      signs in with no password, and the six collections appear in the library with their files.
- [ ] Each seeded file page shows its credit and licence.
- [ ] Every write route answers `403 demo_read_only`, and the WebSocket is refused.
- [ ] `docker compose exec backend python -m app.scripts.assert_inert` (through
      `./opentr.sh shell backend` in a checkout) reports no task published after browsing every page.
- [ ] Database row counts do not change after browsing, apart from sign-in sessions.
- [ ] Two visitors behind the proxy are limited separately (the Cloudflare live check above).
- [ ] The finished conversations open in the chat sidebar, with citations; sending a message
      is refused.
- [ ] `systemctl list-timers 'opentranscribe-demo-*'` shows both timers, and
      `scripts/demo/demo-reset.sh --dry-run` and `scripts/demo/demo-upgrade.sh --dry-run`
      print their plans.
- [ ] After a reset (run it once by hand), the site is open again and the library is seeded.
- [ ] `demo-status.json` exists after the first upgrade and has the contract below.
- [ ] Both alarms and the budget exist, and a test alarm (`aws cloudwatch set-alarm-state`)
      reaches the mailbox.
- [ ] An upgrade from one pre-release to the next succeeds, and a broken one rolls back.

### The `demo-status.json` contract

`demo-upgrade.sh` writes it, in the install directory (or `DEMO_STATUS_FILE`), after each
upgrade attempt, and the API reads it (mounted read-only) for the login page. The release
`finish` stage can poll it through `DEMO_STATUS_CMD` or `DEMO_STATUS_FILE`.

```json
{"tag": "v0.7.0", "previous_tag": "v0.6.1", "result": "upgraded", "at": "2026-11-20T04:12:09+00:00"}
```

`result` is `upgraded`, `rolled-back` (then `tag` is the version now running and `previous_tag`
the one that failed) or `rollback-failed` (the demo is down; nginx stays stopped).

## Rollback

An upgrade rolls itself back when the new version's `/health` does not answer, then reseeds.
To roll back by hand (for example after a bad release that passed `/health`):

```bash
sudo systemctl stop opentranscribe-demo-upgrade.timer     # stop it re-upgrading
cd /opt/opentranscribe
./opentranscribe.sh update --rollback && ./opentranscribe.sh update-full
DEMO_SEED_BUNDLE=/opt/opentranscribe-demo-seed/current scripts/demo/demo-reset.sh
```

Pin the good release with `DEMO_TARGET_TAG=<tag>` in the service environment before enabling the
timer again. Because every upgrade ends in a reset from the seed, the one-way migration chain
never blocks a rollback. Exit code 4 from the upgrade script means the rollback also failed:
the site is closed on purpose, so fix the cause and run the reset.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Site closed after a reset, nginx stopped | A reset step failed on purpose. Read `journalctl -u opentranscribe-demo-reset.service`, fix, rerun `demo-reset.sh`. |
| Reset takes more than 20 minutes | The search model is still loading; the `up --wait` allows `DEMO_UP_TIMEOUT` (900 s). If it times out, check memory (`free -m`): resize to `t4g.xlarge`. |
| `demo-reset.sh` exits 3 | Another reset or upgrade holds the lock. Wait; do not delete the lock while one runs. |
| All visitors share one rate limit | The Cloudflare overlay is not in use: `DEMO_MODE=true`, the nginx overlay and `nginx/cloudflare-real-ip.conf` must all be present. Run `./opentranscribe.sh update-full`, refresh with `scripts/demo/refresh-cloudflare-ips.sh`. |
| Cloudflare 525 or 526 | Origin certificate missing or wrong: `nginx/ssl/server.crt` and `.key` must be the Origin CA pair for the hostname, SSL mode Full (strict). |
| Cloudflare 522 | The security group does not allow Cloudflare's ranges (refresh the prefix list), or nginx is stopped. |
| Seeder refuses | `DEMO_USER_EMAIL` or `DEMO_USER_PASSWORD` is not set, or the bundle fails `demo_seed validate`; the message names the item. |
| Upgrade never fires | `systemctl list-timers`; the host needs outbound access to `api.github.com`; a `DEMO_TARGET_TAG` pin stops following latest. |
| Disk alarm | Old images: `docker image prune -f`. |

## Checks before going public

- Every write route answers `403 demo_read_only` and the WebSocket is refused.
- `python -m app.scripts.assert_inert` reports no task published after browsing every page.
- Database row counts do not change after browsing, apart from sign-in sessions.
- Two visitors behind the proxy are limited separately.
- An upgrade from one pre-release to the next succeeds, and a broken one rolls back.
