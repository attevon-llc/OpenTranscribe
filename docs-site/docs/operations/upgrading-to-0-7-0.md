---
sidebar_position: 5
title: Upgrading to 0.7.0
description: What to expect when you upgrade an existing OpenTranscribe install to 0.7.0, and the changes that need your attention first.
---

# Upgrading to 0.7.0

Back up, update, and the backend brings the database up to date on its own when it starts.
This page lists the changes that can stop an existing install from working until you adjust a
setting, so that nothing surprises you. They are marked **Check this if...**.

## Before you upgrade: back up

```bash
# An installed deployment
./opentranscribe.sh backup

# A git clone of the repository
./opentr.sh backup
```

See [Backup & Restore](./backup-restore.md). Then update:

```bash
./opentranscribe.sh update                    # pull the new images and restart
./opentranscribe.sh update --version v0.7.0   # or pin this release explicitly
```

## Hardened startup checks

**Check this if...** your `.env` predates the current installer, or you wrote it by hand.

A production install refuses to start when `FLOWER_PASSWORD` is unset or a default
(`flower`, `changeme`, a `CHANGE_ME` placeholder), or when the bundled object store does not
encrypt objects (`MINIO_KMS_AUTO_ENCRYPTION` is set to anything but `on`). An install made by the current
installer already has both. Otherwise add these lines to `.env` **before** you run the update:

```bash
FLOWER_PASSWORD=<output of: openssl rand -hex 16>
MINIO_KMS_SECRET_KEY=opentranscribe-key:<output of: openssl rand -base64 32>
MINIO_KMS_AUTO_ENCRYPTION=on
```

From 0.7.0 on, `./opentranscribe.sh update` checks these settings and stops before changing
anything when one is missing. The script you upgrade *with* is your current one, which does not
have that check yet, so on this first upgrade add the lines yourself first; otherwise the backend
refuses to start and its log names the missing setting.

A missing or empty `FLOWER_PASSWORD` is now also refused by Docker Compose itself: the
production configuration no longer falls back to the development password `flower`, so every
`./opentranscribe.sh` command that loads it, `stop` included, ends with
`FLOWER_PASSWORD must be set in .env for production` until you add the line.

Back up `MINIO_KMS_SECRET_KEY` somewhere safe: objects written from then on are encrypted with
it, and if it is lost they cannot be read again. Objects stored before encryption was turned on
stay readable. To keep storing objects unencrypted on purpose, set
`MINIO_KMS_AUTO_ENCRYPTION=off` and `ALLOW_UNENCRYPTED_OBJECT_STORE=true` instead of the two
encryption lines. See [environment variables](../configuration/environment-variables.md).

Object-store encryption is now **on by default** in every install, development included:
`MINIO_KMS_AUTO_ENCRYPTION` counts as `on` when `.env` does not set it, and MinIO cannot start
without `MINIO_KMS_SECRET_KEY`. An `.env` with neither line (written by hand, or trimmed) now
needs the key, or the two opt-out lines above. From 0.7.0 on, `./opentranscribe.sh start` (and
`update` on a production install) stops with these instructions before MinIO is started. It never
invents a key for an existing install, because a key you have not backed up puts every object
written with it at risk.

## LDAP certificate verification

**Check this if...** you sign in through LDAP over `ldaps://` or StartTLS.

The directory's TLS certificate is now verified by default. A directory that presents a
private or self-signed certificate stops accepting sign-ins after the upgrade until you tell
OpenTranscribe how to trust it, in Settings, Authentication or in `.env`:

| Setting | `.env` variable | Use it when |
|---|---|---|
| `ldap_ca_cert_file` | `LDAP_CA_CERT_FILE` | The certificate comes from a private CA. Point it at the CA bundle (PEM). |
| `ldap_tls_server_name` | `LDAP_TLS_SERVER_NAME` | You connect by IP address or an alias, but the certificate lists a different name. |
| `ldap_tls_skip_verify` | `LDAP_TLS_SKIP_VERIFY` | Last resort: turns verification off and logs a warning on every connection. |

A value set in the Settings panel wins over the `.env` variable. A setting left empty in the
panel keeps the `.env` value, and saving other LDAP fields no longer clears it.

LDAP referrals are no longer followed. See [LDAP authentication](../authentication/ldap.md)
for the full setting list.

## MediaCMS sources and plain HTTP

**Check this if...** you import from a MediaCMS server that is served over plain `http://`.

A media source now records its scheme, and sign-in credentials are only ever sent to the
scheme, host and port you configured. An address without a scheme means `https`. A source that
was saved without a scheme and is really served over plain HTTP stops signing in after the
upgrade: edit it in Settings, Media Sources and enter the address as `http://host[:port]`. The
form warns while you type an `http://` address, and the saved source shows an
"Unencrypted (http)" badge.

If two older sources point at the same server, the address of either can only be changed after
one of them is deleted. Their other fields stay editable.

## Which network addresses the ports listen on

**Check this if...** another machine reaches OpenTranscribe through port 5174 (the API),
5183 (the docs) or 5173 (the web UI) instead of the HTTPS address.

In production, the API and docs ports now listen on the server itself (`127.0.0.1`). Browsers
are not affected: they reach both through the web UI's `/api` and `/docs/` paths. When the
HTTPS proxy is set up (`NGINX_SERVER_NAME`), the plain web UI port 5173 also listens on the
server only, so the HTTPS address is the one way in. Without HTTPS, port 5173 keeps listening
on every interface.

| You need, from another machine | Set in `.env` |
|---|---|
| Scripts or tools calling the API port 5174 directly | `BACKEND_BIND_HOST=0.0.0.0` |
| The docs port 5183 directly | `DOCS_BIND_HOST=0.0.0.0` |
| Plain HTTP on port 5173 while HTTPS is also set up | `FRONTEND_BIND_HOST=0.0.0.0` |

Then restart with `./opentranscribe.sh restart`. Only open what you need: these ports skip the
HTTPS proxy, and over plain HTTP sign-in passwords cross the network unencrypted.

An `.env` created from an older template carries `BACKEND_BIND_HOST=0.0.0.0`, which keeps the
API port open on the network. `./opentranscribe.sh update` lists it as a stale value; change
it to `127.0.0.1` unless you need the access above.

New installs ask "Expose directly on your LAN without HTTPS?" when HTTPS is not set up. The
answer defaults to no, which keeps the web UI on the server only; answer yes, or set
`FRONTEND_BIND_HOST=0.0.0.0` later, to use plain HTTP from other devices.

## Settings that moved into the admin UI

Some settings moved from `.env` to the admin UI (**Settings**), so an administrator can change
them without editing a file or restarting. Nothing you configured changes on upgrade.

### What happens to your existing `.env` value

The first time 0.7.0 starts, each variable below that is **set** in your environment is copied
into the database once, and a warning naming the variable appears in the backend log
("`<NAME>` is deprecated"). After that the stored value is the one in use and the variable is
ignored: change it in the UI, not in `.env`. A variable that is not set keeps the built-in
default. The environment read is removed completely in v0.8.0, so delete these lines from
`.env` at your convenience.

| Variable (deprecated) | Now set in | Default |
|---|---|---|
| `YOUTUBE_USER_RATE_LIMIT_ENABLED` | Settings > Recovery > YouTube downloads | on |
| `YOUTUBE_USER_RATE_LIMIT_PER_HOUR` | same | 50 |
| `YOUTUBE_USER_RATE_LIMIT_PER_DAY` | same | 500 |
| `YOUTUBE_AUTO_RETRY_ENABLED` | same | on |
| `YOUTUBE_RECOVERY_BATCH_SIZE` | same | 3 |

Transcript chunk sizing (`SEARCH_CHUNK_TARGET_WORDS`, `SEARCH_CHUNK_OVERLAP_WORDS`) is an
expert setting that changes search results for the whole library; it stays an advanced
environment setting.

### Variables removed because they never had any effect

These were read, then replaced by the per-user or per-deployment value on every transcription,
so setting them did nothing. They are gone from `.env.example` and the compose files; delete
them from your `.env`.

- `VAD_THRESHOLD`, `VAD_MIN_SILENCE_MS`, `VAD_MIN_SPEECH_MS`, `VAD_SPEECH_PAD_MS`: speech
  detection is set per user under Settings > Transcription, and the deployment-wide defaults
  an administrator can change in the same panel (new in 0.7.0).
- `WHISPER_HALLUCINATION_THRESHOLD`, `WHISPER_REPETITION_PENALTY`: same panel.
- `SOURCE_LANGUAGE`: the language is chosen per user and per file.
- `MODELS_DIRECTORY`: no code read it (the model cache is `MODEL_CACHE_DIR`).
- The `FLOWER_USER` and `REDACTION_CONCURRENCY` lines inside the compose `environment:` blocks.
  Both variables still work: they are read by the compose file itself (Flower's login and the
  redaction worker's concurrency).
- The `engine.gpu_split`, `engine.precompute_vad` and `engine.shared_volume_path` database keys
  were never writable from the UI and are no longer read. `ENGINE_GPU_SPLIT` (environment) is
  what decides the split-GPU layout.

### Boolean variables now accept one set of spellings

Every on/off environment variable accepts `true`, `1`, `yes`, `on` and `false`, `0`, `no`,
`off` (any case). Before, about twenty-five of them (for example `ENABLE_OVERLAP_DETECTION`,
`USE_NATIVE_SPEAKER_EMBEDDINGS`, `PIPELINE_SCRATCH_SHARED`, `PRELOAD_GPU_MODELS`,
`ENABLE_API_DOCS`) accepted only the exact word `true` or `false`, so `1` or `no` silently did
the opposite of what was meant. An unrecognised value now logs a warning that names the variable
and uses the documented default. Two keep a deliberately strict reading: `ENGINE_GPU_SPLIT` is
on only for `true` (the start script that adds the split-GPU workers reads the same word), and
`MEDIACMS_VERIFY_SSL` turns certificate verification off only for `false`, `0` or `no`.

### Offline installs: documentation port

`docker-compose.offline.yml` now publishes the documentation on `DOCS_PORT` with default
`5183`, like every other deployment (it was `3030`). Set `DOCS_PORT=3030` to keep the old port.

### Media sources on a private network

A MediaCMS server on a private address (for example `http://10.0.0.5:8080`) is refused by
default, so a user cannot use the server to probe your network. A **super administrator** can
list the exact origins to allow under Settings > Media Sources > Private media hosts. The list
is empty after upgrade, and the cloud-metadata addresses stay blocked whatever it contains. The
old `MEDIACMS_ALLOWED_HOSTS` environment variable only chose which hosts count as MediaCMS
sources (a value without a scheme means `https`); it never permitted a private address and still
does not.
