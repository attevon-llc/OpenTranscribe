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
