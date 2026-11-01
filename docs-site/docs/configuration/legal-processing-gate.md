---
sidebar_position: 7
---

# Required legal documents before processing

An operator can require that the **owner of a file** has accepted the current text of one or
more [legal documents](../legal/legal-documents.md) before anything processes that file. It is
**off by default**. Turn it on in **Settings → Legal → Require acceptance before processing**
(super administrators only).

## Settings

Two database-backed settings, changed in the admin UI with no restart and audited as
`admin.settings.change`:

| Setting | Values | Default |
| --- | --- | --- |
| `legal.processing_required_document_keys` | Comma-separated document keys the active document provider serves | empty (off) |
| `legal.processing_acceptance_scope` | `user`, `organization`, `either` | `either` |

- **user**: only the file owner's own acceptance counts.
- **organization**: only an acceptance recorded on behalf of the file's organization counts. A
  file in a personal workspace has no organization, so it is refused.
- **either**: one of the two.

Matching is on the document's exact text (its SHA-256). When a document's text changes, every
owner must accept the new text before their files are processed again.

The same settings are available over the API: `GET` and `PUT /api/admin/legal-processing-gate`
(super_admin). A `PUT` naming a key the active provider does not serve is refused with 422 and
`{"code": "legal_document_unavailable"}`, and nothing is written.

## Whose acceptance

The subject is always **the file's owner and the file's organization**, never the person who
clicked. An administrator retrying a member's file does not cover the member's missing
acceptance, and an administrator's own acceptance does not cover a member's personal files.

## What is covered

Every way processing of a file can start:

- uploads (the presigned prepare and complete steps, the direct upload), URL and playlist
  imports (refused before anything is downloaded), and watch sources (a refused owner's source
  is skipped before anything is listed, so no file is created only to fail);
- reprocess (every stage, including speaker re-detection), retry, recover and bulk actions;
- the AI steps that run on an existing transcript: summaries, speaker identification, topic
  extraction and redaction detection;
- the transcription dispatch itself, which checks the gate **first**, before any extension
  hook, as a backstop for every path above and for the automatic retry and recovery sweeps.

Maintenance work that does not process new content for a person is not covered: search
re-indexing, embedding migrations and re-embeds, thumbnail and waveform regeneration, exports
and privacy purges.

## Refusals

A route answers with a stable code in `detail.code` and the document keys in
`detail.documents`:

| Code | HTTP | Meaning |
| --- | --- | --- |
| `legal_acceptance_required` | 403 | The owner has not accepted the current text of the listed documents |
| `legal_document_unavailable` | 403 | A required key is not served by the active document provider |
| `legal_gate_unavailable` | 503 | The settings or acceptances could not be read; the gate refused rather than guess |

The gate **fails closed**: a typo in the key list, a removed document or a database error
refuses processing, it never silently turns the requirement off.

A file that was already queued when the gate refused it (a setting switched on mid-flight, or
an automatic retry) is marked **Error** with the code as its message. Automatic retry and
recovery never select such a file again; the user's own Retry works once the document is
accepted. A bulk action reports the code per file in `error` and carries on with the rest.

A changed setting takes effect within about ten seconds in every process (the same cache window
as the privacy write backstop).

## Extensions

The gate reads documents through the document provider seam, so a deployment that installs its
own provider is checked against that provider's documents. A deployment that runs its own
acceptance flow outside the built-in acceptance records should leave the setting empty.
