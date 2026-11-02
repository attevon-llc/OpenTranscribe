---
sidebar_position: 1
title: Legal documents and operator responsibilities
description: The legal documents OpenTranscribe serves, what an administrator acknowledges, and how the hosted edition differs
---

# Legal documents and operator responsibilities

:::caution Drafts pending counsel review
Every document described here is a **draft**. None has been reviewed or approved by counsel yet.
The app shows the draft status next to each document and will keep doing so until a reviewed
version replaces it.
:::

## What the app serves

A self-hosted OpenTranscribe serves four documents, readable by any signed-in user:

| Document | Who it is for |
| --- | --- |
| Operator Acknowledgment | The administrator who completes first-run setup |
| Terms of Use | Operators of self-hosted deployments |
| Privacy Notice | Operators and the people whose data a deployment processes |
| Data Flows Annex | Administrators, procurement and security reviewers |

## What an operator acknowledges

When you set up a deployment you become the operator. The Operator Acknowledgment sets out what
that means: you decide what is recorded and who can reach it, you are responsible for the lawful
basis and notices for the people in your recordings, and you choose which optional third-party
services (cloud speech, LLM providers) receive data. Only a **super administrator** can
acknowledge it. The first-run wizard shows it, and Settings keeps a record of who acknowledged
which version and when.

An acknowledgment is tied to the exact text. The app stores the document key, its version and the
SHA-256 hash of the text you saw. If the text changes in a later release, the new version needs a
new acknowledgment. Acknowledging the same text twice changes nothing.

## What an acceptance records

Each acceptance stores:

| Field | What it is |
| --- | --- |
| Document, version, SHA-256 | Exactly which text was accepted |
| Accepted at | When |
| IP address, user agent | The network address and client that submitted it |
| Locale | The language the text was shown in (from the request, or the browser's `Accept-Language`) |
| Scope | `user` (a person accepted for themselves) or `organization` (see below) |
| Organization | The organization the request was acting in, if any, by id and by uuid |

The audit event written for a new acceptance (`legal.acknowledged`) carries the organization, so
it appears in that organization's audit log. Accepting the same text again writes nothing.

## Accepting for an organization

In the hosted edition an **organization administrator** can accept a document on behalf of the
organization (Settings > Legal > Organization acceptance). The record names the organization and
the administrator who accepted. One acceptance per organization per exact text is kept, and an
administrator of several organizations can accept the same text for each. The Operator
Acknowledgment cannot be accepted at organization scope. An organization administrator can list
the acceptances recorded in their organization; platform administrators see every record.

## Records after an account is deleted

An acceptance is evidence of which terms applied, so it is kept when the account that made it is
deleted, whichever path deletes it (an administrator, a GDPR erasure, or the first-run wizard).
The database does this itself, so no deletion path can skip it. At the moment of deletion:

| Field | After the account is deleted |
|---|---|
| Record id, document, version, hash, time | Kept |
| Scope, organization (id, uuid), locale | Kept. The organization id goes empty if the organization is later deleted; its uuid still names it |
| User | Removed (empty) |
| Account surrogate uuid (`subject_user_uuid`) | Kept |
| IP address, user agent | Removed (empty) |
| Erased at, retain until | Set: the deletion time, and that time plus the retention period |

**The kept record is pseudonymised, not anonymous.** The surrogate uuid can be linked back to a
person by anyone holding a backup, the erasure ledger or exported audit events. It is kept on the
basis of GDPR Art. 17(3)(e) (establishment, exercise or defence of legal claims), together with
Art. 6(1)(c) or (f) for proving which terms applied. OpenTranscribe provides the mechanism; **each
operator states their own lawful basis in their own privacy notice**. This page is not legal
advice.

Retention is the system setting `legal.acceptance_retention_days` (default `2190`, six years;
minimum `365`; a lower value, including `0`, is read as `365`). A daily task
(`legal.acceptance_retention_sweep`, 05:10) deletes records whose retention has ended. The
database refuses any other delete of a record and any rewrite of what was accepted. Administrators
see a deleted account's records in Settings > Legal marked as belonging to a deleted account.

## Where the text comes from

The wording is maintained in one central legal repository. OpenTranscribe copies a pinned release
of those files into `backend/app/legal/documents/` together with a `MANIFEST.json` that records the
source tag, each file's version and its hash. The application never edits the text, and a test
fails if a vendored file no longer matches its recorded hash. Changing a document means changing
it centrally, tagging a new release, and updating the pinned copy.

## How the hosted edition differs

The hosted edition replaces the document set with its own terms through a provider hook in the
backend. When a replacement set is installed, the Operator Acknowledgment is not required, because
the hosted service runs its own acceptance flow. A self-hosted deployment has no such provider and
always requires it.
