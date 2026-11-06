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
encrypt objects (`MINIO_KMS_AUTO_ENCRYPTION` is not `on`). An install made by the current
installer already has both. Otherwise add these lines and start again:

```bash
FLOWER_PASSWORD=<output of: openssl rand -hex 16>
MINIO_KMS_SECRET_KEY=opentranscribe-key:<output of: openssl rand -base64 32>
MINIO_KMS_AUTO_ENCRYPTION=on
```

Back up `MINIO_KMS_SECRET_KEY` somewhere safe: objects written from then on are encrypted with
it, and if it is lost they cannot be read again. Objects stored before encryption was turned on
stay readable. To keep storing objects unencrypted on purpose, set
`ALLOW_UNENCRYPTED_OBJECT_STORE=true` instead of the two encryption lines. See
[environment variables](../configuration/environment-variables.md).

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

LDAP referrals are no longer followed. See [LDAP authentication](../authentication/ldap.md)
for the full setting list.
