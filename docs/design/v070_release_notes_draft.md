# v0.7.0 release notes (draft for owner approval)

Internal draft. Source of truth is the Unreleased section of `CHANGELOG.md`; this is a
condensed, neutral summary. Upgrade steps: `docs-site/docs/operations/upgrading-to-0-7-0.md`.

## Highlights

- **Trash for deleted files.** Deleting a file moves it to a trash page where it can be
  restored; the retention period is 30 days by default and an administrator can set 1 to 90.
  `DELETE /api/files/{uuid}` keeps its route and 204 answer.
- **Legal holds.** Administrators can freeze a file or collection without taking it down.
  A held file's content cannot be edited; the gallery has a Legal hold filter and badge.
- **Organization ownership and scheduled erasure.** An organization has an owner; erasures
  requested by an organization administrator are scheduled and can be cancelled before they run.
- **Multi-track audio.** Choose which audio track of a file is transcribed, see a language
  mismatch notice, and force the transcription language when reprocessing.
- **AI usage limits.** Per-user daily and per-instance monthly LLM token caps in admin settings.
- **Scale and reliability.** Multiple API worker processes with aggregated `/metrics`, bounded
  database waits (503 with `Retry-After` under overload), fair sharing of transcription queues
  between users, many query and startup performance fixes, and a concurrent-user load test.
- **Public read-only demo mode** (`DEMO_MODE=true`).
- **Video.js 10 player** replaces Plyr; SvelteKit 3 and TypeScript 6 on the frontend.
- **Interface.** Right-to-left layout fixes, plural forms in all 12 languages, and a notice that
  non-English translations are machine-generated.

## Fixed

- Outages of Redis, OpenSearch, object storage or the broker no longer read as empty results or
  success in many API, task and service paths; they surface as errors (see upgrade notes for the
  status-code changes).
- LDAP logins with StartTLS were always refused; StartTLS now runs.
- Migrations work through connection proxies that reject startup parameters.
- Tags stay inside the file's organization.
- Amazon Transcribe medical model job parameters.
- Expired refresh tokens are now deleted.

## Security

- Production API and docs ports bind to loopback by default.
- LDAP certificate verification and stricter startup checks.
- Undecryptable stored secrets raise an error rather than reading as "no secret".
- Governance audit events are written in the same transaction as the change, through an outbox.
- Pinned GitHub Actions, dependency updates, and a licence inventory.

## Changed defaults and operator actions

See the upgrade page: separate `migrate` service, loopback port binding, API/DB timeouts,
settings that moved into the admin UI, paged speaker list API, extension import paths
(seam version 14).

## Optional anonymous usage report

Off by default and a double opt-in (`DO_NOT_TRACK=0` plus configuration).

## Open points for the owner

- Confirm the headline order and whether the public demo belongs in the notes.
- Confirm no further migrations land before tagging (latest named in the changelog: v463).
