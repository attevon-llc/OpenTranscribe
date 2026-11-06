---
sidebar_position: 4
title: Upgrading to 0.6.1
description: What to expect when you upgrade an existing OpenTranscribe install to 0.6.1, and the two things that need your attention.
---

# Upgrading to 0.6.1

Upgrading to 0.6.1 is a standard upgrade: back up, update, and the backend applies the migrations
(through v457) when it starts. Two things can need action first.

## LDAP

A plain `ldap://` server address now requires StartTLS or an `ldaps://` address. Binds that would
have been unencrypted are refused. If you use LDAP over plain `ldap://`, enable StartTLS or switch
to `ldaps://` before upgrading. See [LDAP](../authentication/ldap.md).

## S3 object storage

Deleting on a versioned bucket now removes every version. Grant `s3:GetBucketVersioning`,
`s3:ListBucketVersions` and `s3:DeleteObjectVersion`; see [Object storage](./object-storage.md).

## Good to know

- Audit records are protected by a database trigger. Running the backend as an unprivileged role
  (`POSTGRES_APP_USER`, `POSTGRES_APP_PASSWORD`) is optional; see
  [Audit record integrity](./audit-integrity.md).
- Migration v456 adds unique object-key indexes without blocking startup. Shared legacy keys, if
  any, are listed by `GET /api/admin/data-integrity/storage/keys`.
- Self-service erasure, per-file voiceprint retention and required legal documents are off until
  an administrator enables them.
- Legal acceptance records are now kept, pseudonymised, after an account is deleted. See
  [Legal documents](../legal/legal-documents.md).
