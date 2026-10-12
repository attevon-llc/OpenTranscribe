# v0.6.0 work plan: what is done, what is running, what is next

Last updated 2026-10-12. Branch `feat/v0.6.0-frontend-ux`, pull request #1191.
Rule for this milestone: everything lands in v0.6.0, done properly, before the v0.7.0 demo.
The merge to master waits until the AWS agent says go (merge commit, no squash).

Latest UI for review: http://10.10.10.20:5373 (login `admin@example.com` / `password`). The
review stack (`otfresh-v060`) runs the merged code at database version v437, with the mock LLM
and mock ASR overlays up.

## 1. Done and merged into the branch

| Area | What | Issues |
|---|---|---|
| UI review | Filter sidebar (no file size, new order, one Ownership section), gallery toolbar order and icons, list headers, sort arrow, upload modal and tray, notification logo, notification auto-clear policy | #747, #749, #750, #751, #753, #754, #757, #1217 |
| Settings | Transcription and Speaker ID re-layout, sticky tabs, provider card, sidecar status, search, shared slider, Privacy and Redaction under AI and Chat | #861 and the settings review |
| Speakers | Cluster list fix, pyannote.ai key, ignored settings, per-file options | #1192, #1198 to #1205, #1208 |
| Speaker data safety | One shared cleanup for every path that removes or moves a speaker; unassign unlinks the profile and blacklists; every assignment path honours the blacklist | #1214, #1233 |
| Data safety | Deletion leaves no residue, orphan storage sweep and Data Integrity button, test cleanup tooling, test leftovers | #1195, #1211, #1212, #1222 |
| AI switches | Turn off automatic AI speaker names and automatic topics | #1148 |
| Translations | All 12 languages reviewed twice, admin strings translated, guard tests against English left over and dropped accents | #1193, #1220, #1234 |
| Pipeline | One resolver decides what runs where; `GET /user-settings/pipeline`; post-transcription speaker detection stage; any cloud vendor as speaker detector; one merge path; cloud-only lite; per-file provenance | #1224, #1225, #1226, #1228, #1229, #1236 (items 1 to 3) |
| CI | Backend and commit-hook failures reconciled; accessibility allowlist ratcheted | |

## 2. Running now

| Work | Issue | State |
|---|---|---|
| Settings "Processing pipeline" view, speakers-included badges, warnings, reaction to config deletion and non-activation | #1227 (also closes #1219) | Agent integrating with the merged code |
| Docs: architecture diagrams, deployment variants page, drift check | #1230 | Agent building |

## 3. Next, in order

1. Merge #1227 and #1230 when they report.
2. Mock-provider end-to-end run on the review stack (mock ASR and mock LLM are up): real upload, speakers, provenance section, summary.
3. Final gate: full backend suite, frontend suite, full end-to-end, accessibility in light and dark, openapi check, bundle budget, CI green on the final head.
4. Re-capture visual baselines once; walk the UI with the owner at port 5373 (light, dark, Arabic).
5. Close #1218 and #1221 with evidence; update the PR's `Closes` lines.
6. Tidy: merged worktrees, scratch trees, stray processes (ask before deleting).
7. Merge to master when the AWS agent says go.

## 4. Decisions made (do not re-open)

- Transcription and speaker detection are independent, each local or cloud. No forced alignment and no wav2vec2; models with no timestamps are refused for speaker detection.
- Lite is cloud-only for transcription and speaker detection; voiceprints stay local on CPU, behind one interface so the Rust version (#1231, v0.8.0) replaces it unchanged.
- The local speaker detection engine is an admin choice, read-only to users.
- Unassigning a speaker means that section is no longer that person: unlink from the profile (a typed name stays), blacklist, cluster and profile remain for others.
- Notification auto-clear: read successes after 24 hours, unread after 14 days, errors and actionable items never, cap of 100; a setting with a Never option.

## 5. Open items for the owner

- Five product decisions from #1233 to confirm (blacklist off still unlinks; bulk actions refuse a blocked speaker while explicit naming lifts it; split counts as unassign; Re-cluster auto-labels at 0.75 or higher; being blocked from any profile speaker blocks the profile).
- The file detail header and the gallery toolbar rows against the designer's mockup (#1218), if you want the mockup comparison.
- The walkthrough, at the end.
