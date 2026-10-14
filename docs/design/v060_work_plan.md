# v0.6.0 work plan: what is done, what is running, what is next

Last updated 2026-10-14 (round 3, release docs and upgrade safety merged). Branch `feat/v0.6.0-frontend-ux`, pull request #1191.
Rule for this milestone: everything lands in v0.6.0, done properly, before the v0.7.0 demo.
The merge to master waits until the AWS agent says go and the owner has checked the UI (merge commit, no squash).

Latest UI for review: http://10.10.10.20:5373 (login `admin@example.com` / `password`). The
review stack (`otfresh-v060`) runs the merged code with the mock LLM and mock ASR overlays up.

## 1. Done and merged into the branch

| Area | What | Issues |
|---|---|---|
| UI review | Filter sidebar, gallery toolbar, list headers, sort arrow, upload modal and tray, notifications and their auto-clear policy | #747, #749, #750, #751, #753, #754, #757, #1217 |
| UI rounds 2 and 3 | Navigation and status shell, consistency pass (contrast tokens, weights, one accent), one form-field stylesheet, one modal scale, one date-range popover, one slider, pipeline arrows, file-status back-navigation, no purple | #1243, #1244 |
| Captions | One broadcast-rule segmenter for the player track and every download; per-script line breaking (CJK kinsoku, katakana and counters, Thai, long names); WebVTT readable by ffmpeg; real multilingual rendering checked | #1244 |
| Settings | Transcription and Speaker ID re-layout, sticky tabs, settings open instantly with lazy sections, provider card, search, Privacy and Redaction together | #861 and the settings review |
| Speakers | Cluster list fix, per-file options, ignored settings, pyannote.ai key | #1192, #1198 to #1205, #1208 |
| Speaker data safety | One shared cleanup for every path that removes or moves a speaker; unassign unlinks and blacklists; every assignment path honours the blacklist; corrections survive a re-run; a name comes only from a person or a voiceprint at 0.75 or more | #1214, #1233 |
| Upgrade safety | Migration `v439` keeps the names of provably voice-matched legacy speakers; `update` and `update-full` recommend a backup before teardown; API no longer labels an unsourced suggestion as a voice match | #1233 |
| Data safety | Deletion leaves no residue, orphan storage sweep and Data Integrity button, test cleanup tooling | #1195, #1211, #1212, #1222, #1237 |
| Redaction | A sticky CUDA fault no longer fails every later toxicity scan: the worker reloads the model on CPU and stays there until restarted | found on the review stack |
| Pipeline | One resolver decides what runs where; `GET /user-settings/pipeline`; post-transcription speaker stage; any cloud vendor as speaker detector; cloud-only lite; per-file provenance; Settings "Processing pipeline" view | #1219, #1224 to #1229, #1236 (items 1 to 3) |
| Translations | All 12 languages reviewed, guard tests against English left over | #1193, #1220, #1234 |
| Release docs | CHANGELOG `[Unreleased]` by theme, "Upgrading to 0.6.0" page, release post draft, public-safe downstream change list | |
| CI | Platform-bypass discipline test fixed; backend and commit-hook failures reconciled; visual baselines re-captured for round 3 | |

## 2. Running now

Nothing is running except CI on the pull request head. Both pre-commit tiers pass locally, and 2706
migration, speaker, caption, redaction, provenance and pipeline tests pass with no failures.

## 3. Next, in order

1. Green CI on the final head (backend tests take 20 to 45 minutes).
2. Owner walkthrough at port 5373 in light, dark and Arabic: captions in the player and downloads,
   form controls and the date-range popover, settings prefetch, pipeline arrows, file-status
   back-navigation, no purple, select consistency.
3. Downstream adoption is deferred to a later session: the public-safe list and its adoption checklist are in `docs/design/v060_downstream_changes.md`.
4. AWS go-ahead, then the owner merges: `! gh pr merge 1191 --merge`.
5. After the merge: confirm #1218, #1221, #1236, #1237, #1243 and #1244 are closed and #1238 to #1240 stay open.
6. Release through `scripts/release.sh` (never by hand). `preflight 0.6.0` passes; `rehearse` needs the
   live stack stopped and a HuggingFace token in `scripts/release-tests/.env.test-secrets`.
7. Tidy: merged agent worktrees and branches, scratch under `/tmp` (ask before deleting).

## 4. Decisions made (do not re-open)

- Transcription and speaker detection are independent, each local or cloud. No forced alignment and no wav2vec2; models with no timestamps are refused for speaker detection.
- Lite is cloud-only for transcription and speaker detection; voiceprints stay local on CPU.
- The local speaker detection engine is an admin choice, read-only to users.
- Unassigning a speaker means that section is no longer that person: unlink from the profile, blacklist, cluster and profile remain for others.
- A speaker's name comes only from a human or a voiceprint match at 0.75 or more; an AI suggestion is only a suggestion.
- Notification auto-clear: read successes after 24 hours, unread after 14 days, errors and actionable items never, cap of 100.
- The upgrade is a standard one: back up first, migrations run on their own; installs with organizations run two one-off backfill commands.

## 5. Open items for the owner

- The walkthrough, at the end.
- The speaker palette keeps its violet swatches (flagged earlier); CUI classification banners stay purple for regulatory reasons.
- Follow-ups filed on v0.6.0 and left open on purpose: #1238 (capture wrapper reseeds), #1239 (small-business LAN install test), #1240 (email-not-configured state).
- Unused locale keys (`sourceVoiceProfile`, `aiSuggestedBasedOnProfile`) and the `IdleTimeoutDialog` custom chrome remain.
