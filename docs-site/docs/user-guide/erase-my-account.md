---
sidebar_position: 10
title: Erase my account
description: How to ask for your account and everything you own in it to be permanently deleted, what the waiting period is, and how to cancel
---

# Erase my account

If your administrator has turned it on, you can ask for your own account to be permanently deleted
from **Settings > Profile > Erase my account**. If you do not see the card, self-service erasure is
off on this deployment; ask whoever runs it.

## What gets deleted

Your account and everything you own in it: recordings and their stored files, transcripts, speaker
profiles and voiceprints, comments, tasks, chat conversations, prompts, vocabulary, watch sources and
settings. Items you created in an organization that belong to the organization (shared tags and
collections) stay, with your name removed from them.

Two things are kept, and the receipt says so:

- **Recordings under a legal hold** stay until the hold ends. The rest of your data is erased on
  schedule, and the account itself is removed once the last held recording is released.
- **Records of having accepted the terms** are kept in a pseudonymised form for the retention period
  your administrator set (the link to you and your network details are removed).

## Asking for it

1. Open **Settings > Profile** and find **Erase my account**.
2. Type your password (or a current code from your authenticator app if your account uses one).
3. Type `ERASE` to confirm and press **Request erasure**.

A request needs you to prove it is you again, so a stolen session cannot erase an account. An account
that signs in only through an external identity provider and has no authenticator app cannot confirm
here; ask the operator.

You cannot request erasure if you are the last active super administrator.

## The waiting period

Nothing is deleted immediately. The request is scheduled for a date shown on the card (14 days by
default; the administrator can set 0 to 21). Until then:

- Your account keeps working normally, and a notice at the top of the app reminds you.
- Signing in does **not** cancel the request. Only **Cancel erasure request** does, and only before
  the scheduled time.
- At the scheduled time the erasure runs on the next pass of the background reconciliation job, so it
  can start up to one pass interval (daily by default) after the date shown.

## Your request receipt

When you ask, you get a receipt: a reference (the request's ID), when you asked, when it is
scheduled, and a sha256 digest of those fields. Keep it; **Download receipt (JSON)** saves it. It
contains no personal data. After the erasure runs, the operator can show you the erasure receipt for
the same reference, which counts what was removed from each store.

## If you use single sign-on

Erasing your account here does not delete you at your identity provider. If your provider re-creates
accounts automatically at sign-in, signing in again makes a **new, empty** account. Ask your
organization to remove you from the provider as well.
