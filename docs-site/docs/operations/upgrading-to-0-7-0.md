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

## AI token limits

**Check this if...** you want to limit how much your AI features can spend, or you see "token
limit" messages after an administrator sets one.

0.7.0 adds limits on the AI tokens used by every AI feature (chat, summaries, topics, speaker
identification and AI redaction): one per user per day and one for the whole server per month,
both in UTC. They are off after the upgrade (0 means unlimited), so nothing changes until an
administrator sets them in **Settings -> AI usage**. Models running on your own server are
counted too; tick "Do not limit models that run on this server" to exempt them.

A request that would go over a limit is refused before it is sent. Chat shows a message, and a
summary, topic or speaker-identification job is marked failed with the reason. Set the daily
limit higher than one long summary, or long recordings can never be summarized.

The same page shows each user their tokens today and over the last 30 days by feature, with an
estimated cost where the model's price is known. Models on your own server show as free; a
`vllm` or `ollama` address on another company's server now shows as "not priced" rather than
free.
