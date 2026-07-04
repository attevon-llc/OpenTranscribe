# OpenTranscribe Release Assistant

You are helping to create a new release for OpenTranscribe. Follow the complete release process documented in `docs/RELEASE_PROCESS.md`.

## Release Information Needed

First, gather the following information from the user:

1. **Release Type**: Major (X.0.0), Minor (0.X.0), or Patch (0.0.X)
2. **Current Version**: Check `VERSION` file for current version
3. **New Version**: Calculate based on release type
4. **Release Title**: Brief description (e.g., "Security Patch", "Multilingual Support")
5. **Branch Name**: The branch containing the changes to release

## Complete Release Checklist

Execute each phase in order. Mark items complete as you go.

### Phase 1: Pre-Release Verification

- [ ] Confirm the feature/fix branch exists and has all changes
- [ ] Build and test frontend container: `docker build --pull --no-cache -t davidamacey/opentranscribe-frontend:latest -f frontend/Dockerfile.prod frontend/`
- [ ] Build and test backend container: `docker build --pull --no-cache -t davidamacey/opentranscribe-backend:latest -f backend/Dockerfile.prod backend/`
- [ ] Run security scans with Trivy and Grype (full output, all severities)
- [ ] Run the full local gate: `./scripts/run-integration-tests.sh` (ungated suite + all RUN_*-gated security suites, both FIPS modes, integration-marked) and the e2e smoke (`./scripts/e2e/run-e2e-smoke.sh`)
- [ ] Commit any remaining code changes on the feature branch

### Phase 2: Version Bump (ALL files must be updated)

Update version in these files:
- [ ] `VERSION` - Format: `vX.Y.Z`
- [ ] `pyproject.toml` - Format: `version = "X.Y.Z"`
- [ ] `frontend/package.json` - Format: `"version": "X.Y.Z"`
- [ ] `frontend/package-lock.json` - Run: `cd frontend && npm install --package-lock-only`

Commit version bump:
```
git add VERSION pyproject.toml frontend/package.json frontend/package-lock.json
git commit -m "chore: Bump version to X.Y.Z

Update version across all project files:
- VERSION
- pyproject.toml
- frontend/package.json
- frontend/package-lock.json

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

### Phase 3: Documentation Updates

- [ ] **CHANGELOG.md**: Add new version section at TOP (after header, before previous versions)
  - Include: Overview, Added, Changed, Fixed, Security (if applicable), Upgrade Notes

- [ ] **Blog Post**: Create `docs-site/blog/YYYY-MM-DD-vX.Y.Z-release.md`
  - Use appropriate template (feature release vs security release)
  - Include: slug, title, authors, tags, truncate marker, highlights, how to update
  - **Authors**: Must use authors defined in `docs-site/blog/authors.yml` (e.g., `opentranscribe`, `davidamacey`)
  - **Tags**: Must use tags defined in `docs-site/blog/tags.yml` (if new tags needed, add them first)

- [ ] **Verify Docs Build**: Build documentation locally to catch errors before commit
  ```bash
  cd docs-site && npm ci && npm run build
  ```
  - If build fails, fix any issues (missing authors, undefined tags, broken links)
  - Do NOT proceed until build succeeds

- [ ] **README.md**: Update if needed (new features, changed installation, etc.)

- [ ] **CLAUDE.md**: Update if needed (new commands, changed workflows)

Commit documentation:
```
git add CHANGELOG.md docs-site/blog/YYYY-MM-DD-vX.Y.Z-*.md
git commit -m "docs: Add vX.Y.Z release documentation

- Update CHANGELOG.md with vX.Y.Z changes
- Add blog post for release announcement

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

### Phase 4: Git Operations

- [ ] Checkout master: `git checkout master`
- [ ] Pull latest: `git pull origin master`
- [ ] Merge with a merge commit (history is a documentation artifact — NEVER squash): `git merge --no-ff <branch-name>`
- [ ] Commit with detailed release message (include summary, highlights, changes, files)
- [ ] Create annotated tag: `git tag -a vX.Y.Z -m "message"`
- [ ] Push master: `git push origin master`
- [ ] Push tag: `git push origin vX.Y.Z`
- [ ] Delete local branch: `git branch -D <branch-name>`
- [ ] Delete remote branch (if exists): `git push origin --delete <branch-name>`

### Phase 5: GitHub Release

Create release with `gh release create` (**NOT as draft**):

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z - Release Title" \
  --latest \
  --notes "$(cat <<'EOF'
## Release Title

Brief description...

### Highlights
- Highlight 1
- Highlight 2

### How to Update

**Docker Compose:**
```bash
docker compose pull
docker compose up -d
```

### Full Changelog
See [CHANGELOG.md](https://github.com/davidamacey/OpenTranscribe/blob/master/CHANGELOG.md)
EOF
)"
```

**IMPORTANT**: Do NOT use `--draft` flag. Release must be published immediately.

- [ ] Create release (NOT draft)
- [ ] Verify it shows as "Latest" in `gh release list`
- [ ] Verify release page: https://github.com/davidamacey/OpenTranscribe/releases/tag/vX.Y.Z

### Phase 6: Deployment

- [ ] User runs: `USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh` (multi-arch; without the remote builder ARM64 falls back to QEMU, 2–3 h vs ~20 min)
- [ ] Verify images on Docker Hub
- [ ] Run final security scans

## Important Reminders

1. **Version files**: ALL four files must be updated - missing any causes inconsistency
2. **Merge commits**: Always merge with `--no-ff` — each commit is a documentation artifact; NEVER squash (squash-merging also breaks git's merged-branch detection, see the stranded-looking chore/cpu-mode-safe-defaults incident)
3. **Annotated tags**: Use `git tag -a` not `git tag` for proper release tags
4. **Blog post**: Required for major/minor releases, recommended for security patches
5. **CHANGELOG**: Required for ALL releases
6. **Commit messages**: Include Claude Code attribution and Co-Author
7. **Docs build**: ALWAYS run `npm run build` in docs-site/ before committing blog posts to catch missing authors/tags

## Troubleshooting

If you need to fix something after tagging:
1. Delete tag: `git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z`
2. Make fixes
3. Recreate tag: `git tag -a vX.Y.Z -m "message"`
4. Push: `git push origin master && git push origin vX.Y.Z`

## Start the Release

Ask the user:
1. What is the release type? (major/minor/patch/security)
2. What branch contains the changes?
3. What is the release title/description?

Then proceed through each phase, confirming completion of each step.
