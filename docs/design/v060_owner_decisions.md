# v0.6.0 owner decisions (2026-10-25)

These answers supersede the "open questions" sections of the five v0.6.0 plans
(`v060_org_content_governance_plan.md`, `v060_privacy_controls_plan.md`,
`v060_docx_pdf_export_plan.md`, `v060_capabilities_and_about_plan.md`,
`v060_perf_and_precommit_health_plan.md`). Implementers follow this file where a plan
disagrees with it.

| # | Decision | Effect on the plans |
|---|---|---|
| 1 | **Only open-source, commercially usable software.** | Every new dependency (code, fonts, models) needs a recorded licence check against this rule. LGPL and permissive (MIT, BSD, Apache-2.0, OFL) are acceptable because they allow commercial use; GPL/AGPL-only, "research/non-commercial" and unclear licences are not. `fpdf2` (LGPL-3.0-only) is accepted for PDF; ship its licence text and keep it dynamically linked (a normal pip dependency). Verify and record the licence of `python-docx`, `uharfbuzz` and every font file before merging. |
| 2 | **Ship the CJK language pack (fonts) in every backend image, as long as it is legal.** | Allowed only under a licence that permits redistribution and commercial use (e.g. SIL OFL 1.1 for Noto/Source Han fonts). Include the licence files in the image and in a `THIRD_PARTY` notice; verify the exact font package licence before adding. |
| 3 | **Governance defaults: use best practice.** | Take the plan's recommended defaults: support-access grants do NOT reveal restricted collections; a collection hold covers files added later and refuses removal of a held file; platform staff cannot release an org hold through the product; a hold also blocks reprocess (transcript edits stay allowed and audited); keep restricted-collection names unique per org with a generic 409. |
| 4 | **Gender inference stays ON by default; it can be switched off (org and user), and users get a notice.** | Pull a minimal user-facing notice into v0.6.0 (not v0.9.0): wherever gender is shown or configured, state that it is inferred automatically from voice and can be turned off, with a link to the switch. The notice text is translated in all 12 locales. The richer consent records stay with #1132. |
| 5 | **#1132 moves to v0.9.0.** | #1132 is the privacy lifecycle for voiceprints (consent records, per-profile expiry, deletion, export). Not needed for the v0.6.0 switches, so re-milestone it to v0.9.0; #1280/#1279 deliver the policy layer, consent hook and the notice now. |
| 6 | **The reranker thread count must be adjustable** for cloud deployments and different systems. | Reverses the plan's "coded constant, not tunable". Make it a deployment setting: environment variable (documented in `.env.example`, settable from a Helm values file) with an `auto` default (e.g. `min(8, cpu_count // 2)`), applied once per process around the scoring call only. Show the effective value read-only in the admin system status. It cannot be live-tunable from the UI because torch latches the thread count per process; say so in the setting's help text. |

## Migration numbering (avoid collisions)
`v444` is the head on master. Allocation order for the v0.6.0 lanes: **privacy `v445`**, then
governance (`v446` onward, one per work package that needs a table), then anything else.
Each implementer re-reads `backend/alembic/versions/` on its own branch before choosing a
number and rebases if another lane merged first. Never autogenerate a migration in a
hot-reloading container.
