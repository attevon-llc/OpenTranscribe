# .github — issue and project conventions

Moved intact from the root `CLAUDE.md`.

- **`gh issue create` bypasses issue-form validation** — the required "Area" dropdown in
  `.github/ISSUE_TEMPLATE/{bug_report,feature_request,task}.yml` only gets enforced by the GitHub
  web UI, not the API/CLI. When creating an issue with `gh issue create`, always pass `--label`
  with a type label (`bug` | `enhancement` | `task` | `documentation`) plus every applicable area
  label from the bank: `backend`, `frontend`, `asr`, `search`, `rag-chat`, `llm-provider`, `gpu`,
  `docker`, `security`, `performance`, `testing`. Check `gh label list` for the current bank
  before inventing a new one — five topic labels (`asr`/`search`/`rag-chat`/`llm-provider`/`gpu`)
  already exist for grouping the ASR/RAG/search issue clusters. An issue with none of these gets
  auto-tagged `needs-triage` by `.github/workflows/label-from-template.yml` — don't rely on that
  as the normal path, it's the fallback for issues that skip the form.
- **A labeled issue is not a tracked issue — it also needs to be on the org Roadmap project.**
  `gh issue create` does not add the issue to a project or set its milestone; that's a second,
  separate step. `.github/workflows/label-from-template.yml` only applies labels, it does not
  touch the project board. After creating (or materially re-scoping) an issue:
  1. Add it to the board: `gh project item-add 1 --owner attevon-llc --url <issue-url>`.
  2. Set `Status` (`Backlog`/`Ready`/`In Progress`/`In Review`/`Done`), `Priority`
     (`P0`–`P3`), and `Epic` (single-select matching the `epic:*` label if one applies, else the
     closest topic) via `gh project item-edit --id <item-id> --field-id <field-id> --project-id
     PVT_kwDOEFrMRc4Bge4t --single-select-option-id <option-id>`. Look up current field/option IDs
     with `gh project field-list 1 --owner attevon-llc --format json` — they are not hardcoded
     here because options get added/renamed over time.
  3. Set `Target` (date field, same `item-edit` pattern with `--date YYYY-MM-DD`) using real
     prerequisites, not a guess — check what the issue's own body says it depends on (`#NNN`
     references) and never date it before an issue it depends on. Two issues that reference each
     other are a dependency chain, not just "related": tag both with a shared `epic:<name>` label
     (`gh label create epic:<name> ...` if one doesn't already exist) so the chain is visible in
     search, not just in the roadmap's date ordering.
  4. If the work is part of an actual release push (not just "eventually"), assign the matching
     `gh issue edit <n> --milestone "vX.Y.Z"` — create the milestone first with `gh api
     repos/attevon-llc/OpenTranscribe/milestones` if it doesn't exist yet. Don't backdate a
     milestone-scoped issue's target ahead of issues it depends on that aren't in that milestone.
  5. **Priority is not the same as sequence.** Every issue in an active build chain can legitimately
     be `P1` — priority says "does this matter for the current push", the `Target` date and the
     `epic:*` label are what encode order and dependency. Don't set `Target` dates by copying a
     sibling issue's date; derive them from the actual `#NNN` cross-references in each issue body.
  The org project is `https://github.com/orgs/attevon-llc/projects/1` ("OpenTranscribe Roadmap"),
  project ID `PVT_kwDOEFrMRc4Bge4t`.
