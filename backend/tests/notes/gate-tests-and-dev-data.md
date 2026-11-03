# backend/tests — a gate test may not depend on the dev deployment's DATA

Moved intact from `backend/tests/CLAUDE.md`.

## ⚠️ A gate test may not depend on the dev deployment's DATA, or on a fixed wall-clock budget

Two failure shapes cost a full day on 2026-09-06 and produced **zero** product bugs between
them. Both are about the test, not the code, and both keep coming back because each instance
looks like a one-off.

**1. Data. A test that reads whatever happens to be in the dev stack is unfailable for one
developer and unpassable for the next.** Own the data or don't assert on it:

- `integration/test_speaker_label_index_drift.py` swept the entire live `transcript_chunks`
  index. It failed on residue an E2E run had left **ten days earlier** — an upload never
  cleaned up, a speaker rename whose Celery index update never landed. Reindexing that row is
  not a fix; the next run leaves new residue. It now creates a **throwaway chunks index**
  (uuid4 name, the REAL mapping, deleted in teardown) plus rows in the session savepoint, and
  carries a negative control that seeds the original defect so the sweep can still be shown to
  fail. The whole-deployment sweep survives as an opt-in **audit** behind `RUN_INDEX_AUDIT` —
  auditing a deployment and testing a code path are different jobs with different pass
  conditions, and merging them makes the gate hostage to accumulated data.
- `e2e/test_visual_regression.py`'s `settings` surface screenshotted the **viewport**, which
  put the gallery *behind* the modal into the baseline — tag chips, speaker chips, media
  cards. Baseline taken against a near-empty library, so it failed at 1.11% (tolerance 0.50%)
  the moment the stack held two files, with the modal itself pixel-identical. Fixed by
  capturing the **element** (`.settings-modal`), the same thing `chat_trace` already did.
  Masking cannot fix that class: card COUNT changes the region's layout.
- ⚠️ **Still outstanding**: `e2e/test_search.py`'s `KNOWN_QUERY = "PyTorch"` is a term
  "present in the standard dev corpus", and its result tests **skip** when absent. That is the
  silent-skip trap — a green run proving nothing. `tests/fixtures/search_corpus.py` already
  self-seeds a corpus for `RUN_SEARCH_QUALITY_TESTS`; that is the pattern to copy.

**2. Time. A fixed timeout calibrated on an idle machine is a bug, because the gate itself is
the load.** 48 pytest workers, 3 Playwright workers and image builds all hit one docker daemon
and one backend. Every one of these passed alone and failed in the full run:

| Was | Now | Measured reality |
|---|---|---|
| Keycloak healthcheck `start_period 60s` + 5×30s = 210s | 900s | **~600s** — `start-dev` re-augments on every start; `JarResultBuildStep` walks the tree calling `File.setReadable` per entry |
| throwaway Postgres `_wait_ready` 30s ×5 files | `throwaway_pg._READY_TIMEOUT` = 180s | 19 function-scoped fixtures racing a daemon that is also building images |
| `docker run alpine` 60s | `_DOCKER_OP_TIMEOUT` = 120s + one retry | same op takes 8.3s idle |
| `search_page` `.search-page` 15s | 30s (matches its conftest siblings) | the div is the page ROOT — its absence means the app SHELL is gone, and that is gated on a 60s auth probe |

Rules that follow: give the constant a **name**, put the **measurement** beside it, and never
copy a budget into a fifth file. Where a timeout means a real leak (a container that may exist),
retry once and then fail loudly naming the object — don't widen and hope.

⚠️ **Neither shape is a flaky test, and treating them as flake is how they survive.** Before
"fixing" a red gate, check whether the assertion is about the code at all.
