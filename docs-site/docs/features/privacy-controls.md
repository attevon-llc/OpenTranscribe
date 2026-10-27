---
sidebar_position: 8
title: Privacy controls (voiceprints and inferred gender)
description: The restrict-only policy that decides whether speaker voiceprints may be created and matched and whether gender may be inferred, who can switch each layer off, what off does, and how to delete the stored data
---

# Privacy controls (voiceprints and inferred gender)

OpenTranscribe can recognize the same person across recordings by their voice, and it can infer a
speaker's gender from their voice. Both work on biometric-style data, so each has a switch. This
page explains the layers of that policy, what turning a switch off does, and the separate,
irreversible action that deletes the data a switch only hides.

This page also seeds the data inventory for issue
[#1132](https://github.com/attevon-llc/OpenTranscribe/issues/1132), the voiceprint privacy lifecycle
(consent records, per-profile expiry, export), which moved to v0.9.0.

## What the two switches control

| Switch | When on (the default) | When off |
|---|---|---|
| **Voiceprint matching** (cross-file speaker recognition) | Each speaker in a recording gets a voiceprint. New speakers are matched against saved voiceprints, name suggestions come from those matches, and speakers are grouped into clusters across recordings. | No voiceprint is created, stored, matched, suggested from or clustered. Speakers are still told apart **within** a recording (`SPEAKER_00`, `SPEAKER_01`), because that uses temporary data that is never stored. |
| **Inferred gender** | A model infers male or female for each speaker from the voice (and the speaker-name step may compare that with a first name). | Nothing is inferred, shown or used. A gender a person confirmed by hand is theirs and is kept. |

Gender inference is **on by default**. Wherever gender is shown or configured, the interface says
that it is inferred automatically from voice and can be turned off, and links to the switch.

## The four layers

The two questions (may voiceprints exist, may gender be inferred) are answered for one person in
one organization (or their personal workspace) by combining four layers. The policy is
**restrict-only**: every layer can switch a feature off, and no layer can switch on something a layer
above it turned off. Any layer that is off turns the feature off.

| Layer | Who sets it | Where | Default |
|---|---|---|---|
| **Platform** | The operator of a managed edition, through an extension | Not a screen. A derived deployment registers a resolver at an extension seam | No opinion |
| **Instance** | A super admin | Settings, Privacy & Redaction, **Policy** tab, card "Speaker recognition and inferred attributes" | Both on |
| **Organization** | An organization administrator | Settings, organization group, **Privacy policy** (managed editions only; a community install has no organizations) | Both on; an organization with no saved choice inherits |
| **User** | Each person | Voiceprint switch: Settings, Speaker Identification, "Recognize speakers across my recordings". Gender switch: the same page, Voice Attributes, "Predict gender from voice" | Both on |

The first layer that is off is reported as `locked_by` (`platform`, `instance`, `organization`,
`user`, or `consent`), so the interface can say who to ask. A user whose organization turned a
feature off sees their switch disabled with the reason, and the API refuses to change it
(409 `policy_locked`).

**Fail closed.** Every error path reads as "off". A platform resolver that raises, a policy read that
fails, a recording that cannot be found, or a consent hook that raises or returns anything but
`True` denies the feature rather than allowing it.

**Consent.** Core ships no consent record. An extension can register a consent hook that must
approve creating a voiceprint, and approve letting someone other than the owner match against a
saved profile. With no hook registered, consent allows. A denial by a hook is reported as
`locked_by: "consent"` for voiceprints. Consent records and their user interface are part of #1132.

## What "off" does

Turning a switch off **hides data and stops new data. It never deletes anything.** Switching the
feature back on makes existing data visible again, and new recordings get voiceprints again.
Deleting is the separate [purge](#purging-stored-data).

Each decision is taken for the owner and organization of the record in question (the recording or
profile), not for whoever is looking at it or whoever started a job.

| Where | What happens while voiceprints are off | What happens while gender is off |
|---|---|---|
| **Pipeline plan** (at upload and reprocess) | The speaker-recognition step is not scheduled. The pipeline view shows an informational notice: speaker recognition across recordings is off for this recording, speakers are still told apart within it. The diarizer is told not to return voiceprint vectors, so they never leave the worker. | The diarization sidecar is not asked for gender. |
| **Entry gates** (every stage, fresh read, no cache) | Each stage that produces or uses a voiceprint checks again when it starts, so a recording already in flight when a switch flipped is stopped too. A stopped stage reports "skipped by policy", not an error. | The same check in the gender attribute task, the sidecar gender write and the speaker-name step. |
| **Write backstop** (every OpenSearch write) | Every write of a speaker, profile or cluster document is checked again. A refused write is logged once per scope and returns without writing. The answer is cached for 10 seconds per process, so a switch change reaches every process within 10 seconds. A failed check denies and is not cached. | Not applicable (gender is stored in the database, not in a voiceprint document). |
| **Reads and serialization** | Voiceprint-based name suggestions are not made. A stored suggestion that a voiceprint match wrote and nobody verified is left out of responses. Cluster lists are empty for that scope. A transcript-derived AI suggestion is still offered. | Inferred gender is returned as `null` wherever a response embeds a speaker (see [Where inferred gender is withheld](#where-inferred-gender-is-withheld)). A profile's gender is voted on by confirmed genders only. |
| **Actions that would create or recompute voiceprints** | The cluster actions re-cluster, analyze outliers, promote, merge and split answer **409** `voiceprints_disabled` with `locked_by`. The admin profile-embedding repair answers the same when every profile is off, and otherwise skips the profiles that are off. | Not applicable. |
| **Batch jobs** | The admin v4 embedding migration, the consistency repair, the speaker merge task's voiceprint averaging, cluster tasks, profile rebuilds, retroactive matching and `batch_speaker_matching` skip scopes that are off and report a `skipped_by_policy` count. | The admin gender migration and the combined speaker analysis skip files whose owner or organization forbids it, and a forced reset leaves those files' values alone. |
| **Clusters** | A speaker is not placed in a cluster, and the cluster reads return nothing for the scope. | Gender no longer influences cluster joins or evictions unless a person confirmed it. |
| **Suggestions and the speaker-name LLM** | Profile and voice suggestions are not computed. | The voice-versus-name gender comparison is not added to the speaker-name prompt. |
| **Sharing** | Another user's profile that you can reach through a shared collection is dropped from your matching candidates when its owner's scope has voiceprints off, or when a consent hook denies sharing. | Not applicable. |

Plan previews follow the same rule as real recordings, with one difference in whose organization
counts. A request that names no recording, such as `GET /user-settings/pipeline` or the preview that
runs before you switch transcription providers, is judged in the organization the request is
working in, so a settings page never shows a speaker-recognition step as enabled while that
organization forbids it. A real recording is judged in the organization stored on its own row,
whichever organization the person who started the job has active.

Two behaviors are independent of the switches. First, the gender comparison is never added to the
speaker-name prompt for a **remote** LLM provider, even when gender inference is on; a local model
(a loopback, private-network or compose-hostname endpoint) still receives it. Second, the
`DIAR_NATIVE_GENDER` environment switch of the diarization sidecar remains an additional hard off.

**What stays visible.** Named speaker profiles, avatars, speaker assignments that a person verified
and genders that a person confirmed are user assertions, not inferred data. Switching a layer off
does not hide them, and a purge does not delete them.

**An admin migration run while a scope is off.** The v4 embedding migration skips files whose scope
has voiceprints off and reports `skipped_by_policy` when it finishes. After the new index goes live
it holds no voiceprints for those scopes. Their old documents stay in the preserved backup index and
are not restored. Rebuild them later, after switching the layer back on, with the existing
re-run speaker identification reprocess stage.

### Where inferred gender is withheld

While gender is off for a recording's owner and organization, a response never carries the
inferred value, wherever it embeds a speaker. A segment embeds its full speaker, so the speaker
list and each segment's speaker are redacted together, by one function
(`redact_response_speakers` in `backend/app/api/endpoints/files/crud.py`). It covers:

- the file detail (`GET /files/{file_uuid}`),
- `GET /files/{file_uuid}/segments`,
- the response of a segment text edit (`PUT /files/{file_uuid}/transcript/segments/{segment_uuid}`),
- the response of a segment speaker reassignment (`PUT /transcripts/segments/{segment_uuid}/speaker`).

The stored attribute data keeps the inference under three keys of `attribute_confidence`: `gender`
(the model's confidence), `alignment` and `alignment_hint` (the voice-versus-first-name comparison,
which is itself derived from the inferred gender). All three are withheld together, and the
alignment fields the API serves (`gender_alignment`, `gender_alignment_hint`) disappear with them.
The [purge](#purging-stored-data) drops the same three keys.

## Purging stored data

A purge deletes what a switch only hides. It is **irreversible**.

**Targets.** Choose `voiceprints`, `inferred_gender` or both.

| Target | Deleted | Kept |
|---|---|---|
| `voiceprints` | Speaker, profile and cluster-centroid documents in every speaker index (the legacy backup index included). Speaker match rows. Unpromoted clusters and their members. Unverified voiceprint-based name suggestions (the affected speakers' status and label are refreshed and their recordings are queued for a search reindex). Each profile's embedding count and last-update time are reset. | Profiles, names, avatars, speaker assignments a person verified, and the records of promoted clusters (their centroid documents are deleted) |
| `inferred_gender` | On speakers whose gender no person confirmed: the predicted gender, the three `attribute_confidence` keys `gender`, `alignment` and `alignment_hint`, and the time the attributes were predicted | Genders a person confirmed, and the gender stored on a speaker profile |

**A gender purge does not clear a profile's gender.** `speaker_profile.predicted_gender` is set only
when a person confirms a gender, so it is a person's statement and not an inference. The
`inferred_gender` target leaves it in place, and it is not deleted by any purge. Only the values the
model inferred on individual speakers are removed.

**Confirmation.** The request must carry the literal `"confirm": "PURGE"`. The interface makes the
person tick a box and type `PURGE`. The API answers 422 to anything else.

**Switch first.** A target is refused with **409** `policy_still_enabled` unless its switch is off
at the purging layer or above, because otherwise new data would grow straight back. A purge counts
the layers from the instance down to the purging scope: an organization administrator can purge
once the organization or the instance has the feature off, and a person can purge their own data
once their own switch, their organization or the instance has it off. A person whose organization
turned a feature off can therefore still delete their own data, though they can no longer change
their own switch. The check fails closed, and the background task checks it again when it starts.

**Two passes.** The task deletes once, then repeats the same deletion about 60 seconds later. The
second pass catches a write that raced the switch (the write backstop caches for up to 10 seconds).
The task stays `in_progress` until the second pass ends. If a store could not be fully purged, the
task ends `failed`: run the purge again.

**Backups and snapshots.** A purge deletes from the live database and search indexes only. Database
backups and OpenSearch snapshots keep their copies until they rotate out, so a restore can bring
purged voiceprints back. After restoring a backup, run the purge again.

**Who may purge what.**

| Scope | Who | Route | What it covers |
|---|---|---|---|
| Your organization | An organization administrator | `POST /api/org-admin/policy/purge` | Every member's data stamped with the organization |
| Yourself | Any signed-in user | `POST /api/privacy/me/purge` | Your own data in the active scope (the organization you are working in, or your personal workspace) |
| Personal workspaces | A super admin | `POST /api/admin/privacy-policy/purge` with `"scope": "personal"` | Every user's personal-workspace data, never an organization's |
| Everything | A super admin, **single-tenant deployments only** | The same route with `"scope": "all"` | Literally all speaker data on the instance. In a multi-tenant deployment (any active organization, or `TENANCY_MODE=multi`) this answers **403** `purge_scope_not_allowed`: a platform administrator must not destroy tenant data, and each organization purges its own |

Every purge writes audit events (see below) and records counts only, never a name.

## API reference

All routes are under `/api`. Request and response shapes are in `backend/openapi.json`.
Error bodies that carry a `code` look like `{"detail": {"code": "...", ...}}`.

### Effective policy

`GET /privacy/effective` (any signed-in user). Optional query `file_uuid`: the answer is for that
recording's owner and organization. A recording you cannot see, or one that does not exist, is a
404 exactly like the file page. Without it, the answer is for you in your active organization.

```json
{
  "organization_uuid": "5b1f0c7e-...",
  "voiceprint_matching": { "enabled": false, "locked_by": "organization" },
  "gender_inference": { "enabled": true, "locked_by": null }
}
```

`organization_uuid` is `null` for personal scope. `locked_by` is `null` exactly when `enabled` is true.

### Your own voiceprint switch

`GET /privacy/me` and `PUT /privacy/me`. Refused under a support-access grant.

```json
{ "voiceprint_matching_enabled": true, "locked_by": null }
```

`PUT` takes `{"voiceprint_matching_enabled": false}` (a real boolean; `"no"` or `0` is a 422) and
returns the same shape. `locked_by` is `platform`, `instance` or `organization` when a layer above
you has the feature off; `PUT` then answers 409 `{"code": "policy_locked", "locked_by": "..."}`.

### Your own gender switch

The gender switch is the existing per-user speaker attribute setting, `GET /user-settings/speaker-attributes`
and `PUT /user-settings/speaker-attributes`. Both resolve for the organization the request is working
in (the personal workspace when there is none).

```json
{
  "detection_enabled": true,
  "gender_detection_enabled": false,
  "gender_locked_by": "organization",
  "age_detection_enabled": true,
  "show_attributes_on_cards": true,
  "auto_llm_speaker_identification": true
}
```

`gender_detection_enabled` is the **effective** value: it is false when a layer above you forbids
gender inference, whatever you chose, and also false when you turned it off yourself. Your stored
choice is untouched, so it applies again once the restriction is lifted. `gender_locked_by` is
`platform`, `instance`, `organization` or `null`. Your own switch is never reported as a lock, so it
is `null` when only you turned the feature off. It is read-only: a `PUT` ignores it, and a `PUT`
stores your choice even while a layer above has the feature off (unlike the voiceprint switch, it
does not answer 409).

### Instance layer (super admin)

`GET /admin/privacy-policy` and `PUT /admin/privacy-policy`.

```json
{ "voiceprint_matching_enabled": true, "gender_inference_enabled": true }
```

`PUT` is partial (send either or both; at least one is required, and values must be booleans). Each
changed switch is audited as `admin.settings.change`.

### Organization layer (organization administrator)

`GET /org-admin/policy` and `PATCH /org-admin/policy`. The organization is always the caller's own;
there is no organization parameter. Gated by the `org.defaults` capability: when it is off the route
answers 404, even for a platform superuser. Refused under a support-access grant. A community
install has no organizations, and the capability is off, so these routes answer 404 there.

```json
{
  "voiceprint_matching_enabled": true,
  "gender_inference_enabled": true,
  "instance": { "voiceprint_matching_enabled": true, "gender_inference_enabled": true },
  "effective": {
    "voiceprint_matching": { "enabled": true, "locked_by": null },
    "gender_inference": { "enabled": true, "locked_by": null }
  },
  "redaction_floor": {
    "forced_categories": [],
    "forced_pii_entities": [],
    "forced_custom_words": [],
    "force_toxicity_threshold": null,
    "force_export_redacted": false,
    "force_redact_before_llm": false
  },
  "updated_at": null
}
```

`voiceprint_matching_enabled` and `gender_inference_enabled` are the organization's own switches;
`instance` shows what the deployment allows (a switch the operator turned off cannot be turned on
here); `effective` is what applies once the layers above are counted. `updated_at` is `null` until an
administrator saves a policy. `redaction_floor` is `null` while the `redaction.policy` capability is
off for the request.

The organization redaction floor has an API but **no screen in this release**: the organization
privacy panel shows the two switches only. A floor screen is part of
[#1279](https://github.com/attevon-llc/OpenTranscribe/issues/1279), planned for v0.7.0. Until then
an administrator sets a floor through the API, and the stored floor is combined with the
deployment's floor (strictest wins).

`PATCH` is partial and needs at least one field: `voiceprint_matching_enabled`,
`gender_inference_enabled` and/or `redaction_floor`. Sending `redaction_floor` while
`redaction.policy` is off answers 404. A floor can only **add** to the deployment's floor (see
[Content redaction](./content-redaction.md)). Validation errors are 422: categories must be
`pii`, `toxicity` or `profanity`; PII entities must be supported entities; custom words are at most
200 words of at most 100 characters; the toxicity threshold is between 0 and 1 (an explicit `null`
clears it); other floor fields do not accept `null`. Each `PATCH` writes an `org.policy.change`
audit event listing field **names** only, never values, because a custom word can be sensitive.

### Admin migration and consistency checks

The admin migration and consistency routes report what the policy makes a batch job skip. The
policy adds fields to existing routes and creates no new route.

`GET /embeddings/migration/status` (super admin) carries `policy_skipped`, so the admin sees what
a migration would skip before starting it:

```json
{
  "files_total": 240,
  "files_skipped": 31,
  "scopes_total": 12,
  "scopes_skipped": 2,
  "locked_by": ["organization"]
}
```

`files_total` counts completed recordings, and `files_skipped` those whose owner and organization
have voiceprints off. `locked_by` lists the layers responsible.

`POST /embeddings/migration/start` answers **409** `{"code": "voiceprints_disabled", "locked_by":
"<layer>"}` only when **every** recording is in a forbidden scope, because then nothing could be
migrated. Otherwise it starts, and its response carries the same `policy_skipped` object (only when
something is skipped) naming what the run will leave out.

The embedding consistency check reports `skipped_by_policy` in the orchestrator's return value and
its stored last run, in the progress payload of a repair, and in the dry-run counts
(`GET /admin/embedding-consistency/counts`). A speaker with no document in a scope whose voiceprints
are off is the policy working, not damage, so it is not counted in `main_missing` or `v4_missing`
and no repair is queued for it.

### Purge routes and status

Request bodies for the three purge routes:

```json
{ "targets": ["voiceprints", "inferred_gender"], "confirm": "PURGE" }
```

`POST /admin/privacy-policy/purge` also needs `"scope": "personal"` or `"all"`. Each answers
**202**:

```json
{ "task_id": "3f6c1c0e-..." }
```

Errors: 409 `{"code": "policy_still_enabled", "targets": ["voiceprints"]}` (the listed targets are
still on); 403 `{"code": "purge_scope_not_allowed"}` (scope `all` in a multi-tenant deployment);
422 (`confirm` is not `PURGE`, or no target); 503 when the task could not be queued (nothing was
deleted).

`GET /privacy/purge/{task_id}` returns the status of a purge **you** started. Anyone else's, or an
unknown id, is a 404.

```json
{
  "task_id": "3f6c1c0e-...",
  "status": "completed",
  "counts": { "voiceprint_docs_deleted": 412, "clusters_deleted": 17, "suggestions_cleared": 63 }
}
```

`status` is `pending`, `in_progress`, `completed`, `failed` or `skipped`. `counts` is `null` until
a pass has finished and sums both passes. Counts can include `voiceprint_docs_deleted`,
`matches_deleted`, `clusters_deleted`, `cluster_members_deleted`, `speakers_unclustered`,
`profiles_reset`, `suggestions_cleared`, `statuses_refreshed`, `reindex_queue_failed` and
`gender_speakers_cleared`.

### Audit events

An organization administrator sees the organization's events in **Settings, organization
group, Audit log** (titled "Organization audit log"; managed editions with the `org.audit_logs`
capability, route `GET /org-admin/audit-logs`). The three events below are stamped with the organization, so they
appear there and in its event filter, labelled "Changed the privacy policy", "Deleted stored
voiceprints" and "Deleted inferred gender data".

| Event | When |
|---|---|
| `admin.settings.change` | An instance switch changed (setting name, old and new value) |
| `org.policy.change` | An organization policy `PATCH` (field names only; stamped with the organization) |
| `privacy.voiceprints.purge`, `privacy.gender.purge` | One event per target for each phase of a purge: requested, each pass completed, aborted or failed. Counts only; stamped with the organization when there is one |

## Data inventory

What the two features store, and what each purge target removes. This is the starting inventory for
#1132.

| Data | Store | Created when | Hidden by | Deleted by |
|---|---|---|---|---|
| Speaker voiceprint (one per speaker in a recording) | OpenSearch speaker index (`speakers` alias, `speakers_v3` / `speakers_v4`) | Speaker identification after transcription | Voiceprint switch | `voiceprints` |
| Profile embedding (average of a profile's voiceprints) | OpenSearch speaker index; `speaker_profile.embedding_count`, `last_embedding_update` | A speaker is linked to a profile | Voiceprint switch | `voiceprints` |
| Cluster centroid | OpenSearch speaker index; `speaker_cluster`, `speaker_cluster_member`, `speaker.cluster_id` | Cross-file clustering | Voiceprint switch | `voiceprints` (unpromoted clusters) |
| Speaker match | `speaker_match` | Matching a speaker against others | Voiceprint switch | `voiceprints` |
| Voiceprint-based name suggestion | `speaker.suggested_name`, `suggestion_source` (`voice_match`, `profile_match`), `confidence` | Matching | Voiceprint switch (when unverified) | `voiceprints` (when unverified) |
| Inferred gender and confidence | `speaker.predicted_gender`, `attribute_confidence`, `attributes_predicted_at` | The gender step, or the diarizer sidecar | Gender switch | `inferred_gender` (when not confirmed) |
| Confirmed gender, named profiles, avatars, verified assignments | Database | A person's action | Never | Never by a purge |

## For operators

- **Remote LLM speaker-ID prompts no longer receive the gender cross-reference.** The speaker-name
  step used to add a voice-versus-first-name comparison ("voice: female 92%") to its prompt. It now
  adds it only when the configured LLM is local (a loopback, private-network or compose-hostname
  endpoint); a remote provider gets the prompt without it, even with gender inference on. The
  check fails closed: any ambiguity reads as remote. Sending an inferred biometric attribute to a
  third party is a data-egress event, and this follows the same local-versus-remote rule as masked
  transcript text.
- **A community install has no organization layer.** `org.defaults` is off there, so
  `/api/org-admin/policy` and its purge route answer 404, and only the instance and user layers
  apply.
- **After upgrading an install that uses organizations,** run the tenant backfill described in
  [Upgrading to 0.6.0](../operations/upgrading-to-0-6-0.md#tenancy-mode). It also re-stamps the
  cluster documents of single-organization clusters that an earlier release rewrote without their
  organization. Clusters whose members span scopes stay unstamped until the next re-cluster.

## Related

- [Upgrading to 0.6.0](../operations/upgrading-to-0-6-0.md#privacy-controls) for what changes on an
  existing deployment.
- [Speaker diarization](./speaker-diarization.md) for how speakers are detected.
- [Content redaction](./content-redaction.md) for the redaction floor an organization can add to.
- [Support access](../operations/support-access.md) for `TENANCY_MODE` and support-access grants.
