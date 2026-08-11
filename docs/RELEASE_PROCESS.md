# OpenTranscribe Release Process

> **This file used to be an 864-line manual checklist stamped "Updated for
> v0.4.0".** Releasing is now driven by a script, so the checklist has been
> replaced by a pointer rather than left to drift — three competing checklists
> disagreeing with each other is what made the previous release process
> unreliable in the first place.

## Where the real documentation lives

| For | Read |
|---|---|
| **Running a release** | [`docs-site/docs/developer-guide/releasing.md`](../docs-site/docs/developer-guide/releasing.md) — published at **Developer Guide → Releasing** |
| **Driving it as an agent** | `.claude/skills/release/SKILL.md` |
| **The rehearsal harness** | `scripts/release-tests/` (see `scripts/CLAUDE.md`) |
| **Gate definitions** | `scripts/release/release-criteria.yaml` |

## The 30-second version

```bash
./scripts/release.sh status              # where am I?
./scripts/release.sh reset 0.5.0         # clear rehearsal history before a real run
./scripts/release.sh preflight 0.5.0     # seconds — fails fast
./scripts/release.sh run 0.5.0           # the whole sequence
```

Twelve stages, each independently runnable, skippable and resumable:

```
preflight → bump → verify → test → build → scan → rehearse
          → tag → publish → smoke → promote → finish
```

The last four reach outside this repository and each requires `--yes` **and** its
`ask` rule in `.claude/settings.json`.

Exit codes are stable so an agent can branch on them: `0` pass, `1` gate failed,
`2` misuse, `3` precondition unmet, `4` operator abort.

## Two things that are easy to get wrong

- **Version facts are derived, never recorded.** The Alembic head comes from the
  `down_revision` graph (`scripts/release-tests/lib/alembic-head.py`); FROM/TO
  come from the `VERSION` file and Docker Hub. Do not reintroduce a checked-in
  table of versions — the previous one (`expected-schemas.tsv`) went stale
  precisely because it was hand-maintained and read by nothing.
- **A gate can be overridden, never silently.** `--force-<stage> "reason"`
  records the operator and a **mandatory** reason in the ledger and marks the
  stage `overridden`, not passed. There is deliberately no bare `--force`.
