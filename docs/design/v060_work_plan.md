# v0.6.0 work plan: what is done, what is running, what is next

Last updated 2026-10-11. Branch `feat/v0.6.0-frontend-ux`, pull request #1191.
Rule for this milestone: everything lands in v0.6.0, done properly, before the v0.7.0 demo.
The merge to master waits until the AWS agent says go (merge commit, no squash).

You can see the latest UI at http://10.10.10.20:5373 (login `admin@example.com` / `password`).

## 1. Done and merged into the branch

| Area | What | Issues |
|---|---|---|
| UI review | Filter sidebar (no file size, new order, one Ownership section), gallery toolbar order and icons, list headers, sort arrow, upload modal and progress tray, notification logo, notification auto-clear policy | #747, #749, #750, #751, #753, #754, #757, #1217 |
| Settings | Transcription and Speaker ID re-layout, sticky tabs, provider card, sidecar status, search, shared slider, Privacy & Redaction under AI & Chat | #861 and the settings review |
| Speakers | Cluster list fix, pyannote.ai key, ignored settings, per-file options | #1192, #1198 to #1205, #1208 |
| Data safety | Deletion leaves no residue, orphan storage sweep and Data Integrity button, test cleanup tooling | #1195, #1211, #1212 |
| AI switches | Turn off automatic AI speaker names and automatic topics | #1148 |
| Translations | All 11 languages reviewed by a native-level reviewer agent | #1193 |
| CI | 12 failing backend tests reconciled | |

## 2. Running right now

| Work | Issue | State |
|---|---|---|
| Speaker delete leaves stale cluster voiceprint (privacy, must fix) | #1214 | Agent working in its own worktree. Audits every path that removes or moves a speaker, one shared cleanup, tests first |
| Refuse pyannote.ai speaker detection with local transcription (today it is silently ignored) | #1223 (WP1) | Agent working in its own worktree |

## 3. The pipeline plan (mix and match transcription and speaker detection)

Full plan: `docs/design/v060_pipeline_composition_plan.md`.

Decisions you made:
- Transcription and speaker detection are chosen independently, each local or cloud.
- Lite is fully cloud-only. The only local work on lite is voiceprinting.
- Voiceprints are always extracted locally. Cloud vendors return no usable embeddings. Lite voiceprinting moves to Rust in v0.8.0 (#1231); in 0.6.0 it sits behind one narrow interface so Rust can replace it.
- No wav2vec2 and no forced alignment. Models with no timestamps (`gpt-4o-transcribe`, `gpt-4o-mini-transcribe`) are refused for speaker detection.
- Other vendors can act as the speaker detector, with a clear billing and privacy note.
- The local engine stays an admin choice.

Work packages, in order:

| Step | Issue | What | Depends on |
|---|---|---|---|
| WP1 | #1223 | Interim fix: refuse pyannote.ai with local transcription | running now |
| WP2 | #1224 | One resolver decides what runs where; `GET /user-settings/pipeline`; validation | WP1 |
| WP3 | #1225 | Post-transcription speaker detection stage, migration, voiceprint interface, fixes segment re-split | WP2 |
| WP4 | #1226 | Any cloud vendor as speaker detector (adapter), one merge path | WP2 (parallel with WP6) |
| WP6 | #1228 | Lite is cloud-only: hide and refuse local options, migrate settings, fix docs | WP3 |
| WP7 | #1229 | Per-file provenance: which provider and model did each step, shown on file details and file status | WP3 |
| WP5 | #1227 | Settings "Processing pipeline" view, Local/Cloud chips, speakers-included badges, warnings (absorbs #1219) | WP7 |
| WP8 | #1230 | Docs-site architecture diagrams and pages updated to the new flow, with a drift check | last |

Translation files are edited by several packages, so they merge in this order: WP1, WP3, WP6, WP7, WP5.

## 4. Other open work on the milestone

| Issue | What | Waiting on |
|---|---|---|
| #1220 | Fix the English wording the translators flagged, then re-review those strings | nothing |
| #1222 | Leftovers from the test cleanup work (PKI trap, eval purge, benchmark prefixes, db 15 guard) | your db 15 decision |
| #1221 | Final verification: backend gate, accessibility (light and dark), end-to-end tests, visual baselines, UI walkthrough, cleanup | everything above |
| #1218 | File detail page and toolbar check, then close | quick check |
| #748 | Left open: its file detail header part is covered by #1218 | |
| #753 | Closed by #1217 when the PR merges | |
| #1219 | Closed by #1227 | |

## 5. Waiting on you

- Delete the raw notes file in the main checkout root: `! rm /mnt/nvm/repos/transcribe-app/ux-ui-eng-notes.md`
- Remove old scratch trees: `! rm -rf /tmp/redcheck* /tmp/delres-head-* /tmp/ot-redcheck.*`
- Decide: can the test that uses Redis database 15 clear its own leftover keys at setup, or should the old skip-if-busy guard come back? (#1222)
- Confirm the lite voiceprint plan (CPU embedding in 0.6.0, Rust in 0.8.0) is what you want.
- When it is time: say when the AWS agent is done so the PR can merge.

## 6. Finish line

1. Merge each finished agent branch into `feat/v0.6.0-frontend-ux`, one at a time.
2. After each merge: frontend tests, backend suite on the isolated stack, push, CI green.
3. Run #1221: full verification, walk the UI with you at port 5373, re-capture visual baselines once.
4. Update the PR description so every finished issue has its own `Closes` line.
5. Wait for the AWS agent, then merge with a merge commit and confirm each issue closed.
