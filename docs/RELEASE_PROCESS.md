# OpenTranscribe Release Process
<!-- Updated for v0.4.0 -->

Complete guide for releasing any OpenTranscribe update - features, bug fixes, security patches, or major versions.

## Quick Reference

| Release Type | Version | Branch Pattern | Blog Post | CHANGELOG |
|--------------|---------|----------------|-----------|-----------|
| Major | X.0.0 | `release/vX.0.0` | Required | Required |
| Minor (Features) | 0.X.0 | `feat/description` | Required | Required |
| Patch (Bug Fix) | 0.0.X | `fix/description` | Optional | Required |
| Security | 0.0.X | `sec/description` | Required | Required |

---

## Complete Release Process

### Phase 1: Development Branch

#### 1.1 Create Feature/Fix Branch
```bash
# For new features
git checkout -b feat/feature-name

# For bug fixes
git checkout -b fix/bug-description

# For security patches
git checkout -b sec/security-description

# For major releases
git checkout -b release/vX.Y.Z
```

#### 1.2 Implement Changes
- Write code for features/fixes
- Add/update tests
- Update inline documentation

#### 1.3 Build and Test Locally

**Frontend:**
```bash
cd frontend
npm run build
npm run check  # TypeScript check
```

**Backend:**
```bash
cd backend
pytest tests/
```

**Docker Containers:**
```bash
# Build frontend container
docker build --pull --no-cache -t davidamacey/opentranscribe-frontend:latest \
  -f frontend/Dockerfile.prod frontend/

# Build backend container
docker build --pull --no-cache -t davidamacey/opentranscribe-backend:latest \
  -f backend/Dockerfile.prod backend/
```

#### 1.4 Run Security Scans (Recommended for all releases)
```bash
# Trivy scans
trivy image --format table davidamacey/opentranscribe-frontend:latest
trivy image --format table davidamacey/opentranscribe-backend:latest

# Grype scans
grype davidamacey/opentranscribe-frontend:latest
grype davidamacey/opentranscribe-backend:latest

# Hadolint for Dockerfiles
hadolint frontend/Dockerfile.prod
hadolint backend/Dockerfile.prod
```

#### 1.5 Commit Code Changes
```bash
git add <changed-files>
git commit -m "type(scope): Description

Detailed explanation of changes.

- Bullet point 1
- Bullet point 2

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

**Commit Types** (following [Conventional Commits](https://www.conventionalcommits.org/)):
- `feat`: New feature
- `fix`: Bug fix
- `security`: Security patch
- `perf`: Performance improvement
- `refactor`: Code refactoring
- `docs`: Documentation only
- `chore`: Maintenance tasks
- `test`: Adding or updating tests
- `ci`: CI/CD changes
- `build`: Build system changes

**Pre-commit Hooks:**
All commits will be validated by pre-commit hooks. Ensure:
- Code passes linting (ruff, eslint)
- No secrets detected
- Dockerfiles pass hadolint
- YAML/JSON/TOML files are valid

---

### Phase 2: Version Updates

**All version files MUST be updated for every release.**

#### 2.1 Version File Locations

| File | Format | Example |
|------|--------|---------|
| `VERSION` | Plain text with v prefix | `v0.2.1` |
| `pyproject.toml` | TOML (no v prefix) | `version = "0.2.1"` |
| `frontend/package.json` | JSON (no v prefix) | `"version": "0.2.1"` |
| `frontend/package-lock.json` | JSON (auto-updated) | `"version": "0.2.1"` |

#### 2.2 Update Each File

**VERSION:**
```bash
echo "vX.Y.Z" > VERSION
```

**pyproject.toml:**
```toml
[project]
version = "X.Y.Z"
```

**frontend/package.json:**
```json
{
  "version": "X.Y.Z"
}
```

**frontend/package-lock.json (auto-update):**
```bash
cd frontend && npm install --package-lock-only && cd ..
```

#### 2.3 Commit Version Bump
```bash
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

---

### Phase 3: Documentation Updates

#### 3.1 Update CHANGELOG.md

Add new section at the TOP of `CHANGELOG.md` (after header, before previous versions):

**For Feature Releases:**
```markdown
## [X.Y.Z] - YYYY-MM-DD

### Overview
Brief description of the release highlights.

### Added
- New feature with description
- Another new feature

### Changed
- Changed behavior or updated functionality

### Fixed
- Bug fix description

### Deprecated
- Features being phased out

### Removed
- Features removed in this version

### Upgrade Notes
Any special instructions for upgrading.

---
```

**For Security Releases:**
```markdown
## [X.Y.Z] - YYYY-MM-DD

### Overview
Security patch release addressing vulnerabilities.

### Security
- CVE-XXXX-XXXXX (severity) - Description of fix
- Security improvement description

### Changed
- Container/dependency updates

### Upgrade Notes
All users are encouraged to update immediately.

---
```

#### 3.2 Create Blog Post

Create `docs-site/blog/YYYY-MM-DD-vX.Y.Z-release.md`:

**For Feature Releases:**
```markdown
---
slug: vX.Y.Z-release
title: "OpenTranscribe vX.Y.Z - Release Title"
authors: [opentranscribe]
tags: [release, features, category1, category2]
---

Exciting introduction about the release and what it brings.

<!-- truncate -->

## Highlights

### Major Feature 1
Description of the feature and how users benefit...

### Major Feature 2
Description...

## Other Improvements

- Improvement 1
- Improvement 2

## Bug Fixes

- Fix 1
- Fix 2

## How to Update

**Docker Compose:**
```bash
docker compose pull
docker compose up -d
```

**Manual Docker:**
```bash
docker pull davidamacey/opentranscribe-frontend:vX.Y.Z
docker pull davidamacey/opentranscribe-backend:vX.Y.Z
```

## What's Next

Teaser for upcoming features...

## Full Changelog

See [CHANGELOG.md](https://github.com/davidamacey/OpenTranscribe/blob/master/CHANGELOG.md)

---

Thank you for using OpenTranscribe!
```

**For Security Releases:**
```markdown
---
slug: vX.Y.Z-security-release
title: "OpenTranscribe vX.Y.Z - Security Patch"
authors: [opentranscribe]
tags: [release, security, patch]
---

Security patch release. **All users are encouraged to update.**

<!-- truncate -->

## Why This Update Matters

Explanation of the security context...

## What's Fixed

### Vulnerabilities Resolved

| CVE | Package | Severity | Status |
|-----|---------|----------|--------|
| CVE-XXXX-XXXXX | package | CRITICAL | ✅ Fixed |

### Container Updates

- Update description 1
- Update description 2

## How to Update

**Docker Compose:**
```bash
docker compose pull
docker compose up -d
```

## Our Security Commitment

Statement about security practices...

## Full Changelog

See [CHANGELOG.md](https://github.com/davidamacey/OpenTranscribe/blob/master/CHANGELOG.md)
```

#### 3.3 Update README.md (If Needed)

Update when:
- New major features added
- Installation process changes
- New dependencies required
- Configuration options change
- Version badges need updating

#### 3.4 Update CLAUDE.md (If Needed)

Update when:
- New commands or scripts added
- Development workflow changes
- New services added
- Configuration changes

#### 3.5 Update Technical Documentation (If Needed)

Update the following docs when the corresponding areas change:
- `docs/INSTALLATION.md` — hardware recommendations, new env vars, new deployment modes
- `docs/ARCHITECTURE.md` — worker types, pipeline changes, service versions
- `docs/DOCKER_DEPLOYMENT.md` — new Dockerfile variants, new build flags
- `docs/database-schema.md` — new tables/columns from Alembic migrations
- `docs/BACKEND_DOCUMENTATION.md` — new modules, removed files, version string
- `docs/DEPLOYMENT_CHECKLIST.md` — new pre-deployment steps
- `docs/VERIFICATION_CHECKLIST.md` — new verification items
- `docs/CONTRIBUTING.md` — tooling changes, new development patterns

#### 3.5 Commit Documentation
```bash
git add CHANGELOG.md "docs-site/blog/YYYY-MM-DD-vX.Y.Z-*.md"
# Add README.md, CLAUDE.md if updated
git commit -m "docs: Add vX.Y.Z release documentation

- Update CHANGELOG.md with vX.Y.Z changes
- Add blog post for release announcement
- Update README.md (if changed)

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

---

### Phase 4: Git Operations (Merge, Tag, Push)

#### 4.1 Checkout Master and Update
```bash
git checkout master
git pull origin master
```

#### 4.2 Squash Merge Release Branch
```bash
git merge --no-ff feat/feature-name
# or: git merge --no-ff fix/bug-name
# or: git merge --no-ff sec/security-name
```

#### 4.3 Commit with Detailed Release Message
```bash
git commit -m "release: vX.Y.Z Release Title

## Summary
One paragraph description of what this release contains.

## Highlights
- Major change 1
- Major change 2
- Major change 3

## Changes

### Added
- New feature 1
- New feature 2

### Fixed
- Bug fix 1

### Security (if applicable)
- Security fix 1

## Files Changed
- path/to/file1.ext
- path/to/file2.ext

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"
```

#### 4.4 Create Annotated Tag
```bash
git tag -a vX.Y.Z -m "vX.Y.Z - Release Title

Description of the release.

Highlights:
- Highlight 1
- Highlight 2
- Highlight 3

See CHANGELOG.md for full details."
```

#### 4.5 Push Master and Tag
```bash
git push origin master
git push origin vX.Y.Z
```

#### 4.6 Delete Feature Branch
```bash
# Delete local branch (use -D if merge (--no-ff)d)
git branch -D feat/feature-name

# Delete remote branch if it was pushed
git push origin --delete feat/feature-name
```

---

### Phase 5: GitHub Release

#### 5.1 Create Release with Full Notes

**IMPORTANT**: Do NOT use `--draft` flag. Release must be published immediately.

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z - Release Title" \
  --latest \
  --notes "$(cat <<'EOF'
## Release Title

Brief description of the release.

### Highlights

- **Feature 1**: Description
- **Feature 2**: Description
- **Fix 1**: Description

### What's New

#### Category 1
- Detail 1
- Detail 2

#### Category 2
- Detail 1

### How to Update

**Docker Compose (Recommended):**
```bash
docker compose pull
docker compose up -d
```

**Manual Docker:**
```bash
docker pull davidamacey/opentranscribe-frontend:vX.Y.Z
docker pull davidamacey/opentranscribe-backend:vX.Y.Z
```

### Breaking Changes

List any breaking changes (or "None" for minor/patch releases).

### Contributors

Thanks to everyone who contributed to this release!

### Full Changelog

See [CHANGELOG.md](https://github.com/davidamacey/OpenTranscribe/blob/master/CHANGELOG.md)

---

🎉 Thank you for using OpenTranscribe!
EOF
)"
```

---

### Phase 6: Build and Deploy

#### 6.1 Build and Push Docker Images
```bash
./scripts/docker-build-push.sh
```

This script will:
- Build multi-platform images (amd64, arm64)
- Push to Docker Hub with `latest` and `vX.Y.Z` tags
- Run security scans

#### 6.2 Verify Deployment
- Check Docker Hub for new images
- Verify tags are correct
- Test pulling images

#### 6.3 Update Documentation Site (Optional)
```bash
cd docs-site
npm run build
# Deploy to hosting
```

---

## Release Checklist

Copy and use this checklist for every release:

```markdown
### Release vX.Y.Z Checklist

#### Development
- [ ] Create feature/fix branch
- [ ] Implement changes
- [ ] Write/update tests
- [ ] Build frontend locally
- [ ] Build backend locally
- [ ] Build Docker containers
- [ ] Run security scans
- [ ] Commit all code changes

#### Version Updates
- [ ] Update VERSION file
- [ ] Update pyproject.toml
- [ ] Update frontend/package.json
- [ ] Run npm install --package-lock-only
- [ ] Commit version bump

#### Documentation
- [ ] Update CHANGELOG.md
- [ ] Create blog post
- [ ] Update README.md (if needed)
- [ ] Update CLAUDE.md (if needed)
- [ ] Update technical docs in `docs/` (see section 3.5)
- [ ] Commit documentation

#### Git Operations
- [ ] Checkout master
- [ ] Pull latest from origin
- [ ] Merge (--no-ff) feature branch
- [ ] Commit with detailed message
- [ ] Create annotated tag
- [ ] Push master to origin
- [ ] Push tag to origin
- [ ] Delete feature branch (local)
- [ ] Delete feature branch (remote, if exists)

#### GitHub Release
- [ ] Create release with notes
- [ ] Verify release page looks correct

#### Deployment
- [ ] Run docker-build-push.sh
- [ ] Verify images on Docker Hub
- [ ] Test image pull
- [ ] Run final security scans
```

---

## Troubleshooting

### Forgot to Bump Version Before Tagging

```bash
# Delete tag locally and remotely
git tag -d vX.Y.Z
git push origin :refs/tags/vX.Y.Z

# Make version changes
# ... edit files ...

# Commit version bump
git add VERSION pyproject.toml frontend/package.json frontend/package-lock.json
git commit -m "chore: Bump version to X.Y.Z"

# Recreate and push tag
git tag -a vX.Y.Z -m "message"
git push origin master
git push origin vX.Y.Z
```

### Need to Update Release Notes

```bash
# Edit existing release
gh release edit vX.Y.Z --notes "updated notes"

# Or delete and recreate
gh release delete vX.Y.Z --yes
gh release create vX.Y.Z --title "title" --notes "notes"
```

### Branch Won't Delete (Not Fully Merged)

After merge (--no-ff), the branch appears "not fully merged":
```bash
# Force delete is safe after merge (--no-ff)
git branch -D branch-name
```

### Tag Already Exists Error

```bash
# Delete existing tag
git tag -d vX.Y.Z
git push origin :refs/tags/vX.Y.Z

# Recreate
git tag -a vX.Y.Z -m "message"
git push origin vX.Y.Z
```

---

## Release Notes Best Practices

### Structure

Every release note should include these sections (as applicable):

```markdown
## vX.Y.Z - Release Title

### Highlights (TL;DR)
- 3-5 bullet points of the most important changes
- Users should understand the release value from just this section

### What's New / Added
- New features with brief descriptions
- Link to documentation for complex features

### Improvements / Changed
- Enhanced functionality
- Performance improvements
- UX improvements

### Bug Fixes / Fixed
- Bug fixes with issue references when available
- Format: "Fixed [issue description] (#issue-number)"

### Security (for security releases)
- CVE identifiers with severity
- Affected components
- Mitigation details

### Breaking Changes (if any)
- Clear description of what changed
- Migration steps required
- Deprecation notices

### How to Update
- Docker Compose commands
- Manual Docker commands
- Any special upgrade steps

### Contributors (for community contributions)
- Thank contributors by GitHub username
- Link to their PRs

### Full Changelog
- Link to CHANGELOG.md
```

### Writing Guidelines

1. **Be User-Focused**: Write from the user's perspective, not developer's
   - ❌ "Refactored authentication module"
   - ✅ "Login is now 2x faster"

2. **Be Specific**: Avoid vague descriptions
   - ❌ "Various bug fixes"
   - ✅ "Fixed crash when uploading files larger than 2GB"

3. **Use Action Verbs**: Start items with verbs
   - Added, Fixed, Improved, Updated, Removed, Deprecated

4. **Include Context**: Explain why changes matter
   - ❌ "Updated nginx to 1.29.4"
   - ✅ "Updated nginx to 1.29.4 (fixes 3 security vulnerabilities)"

5. **Link to Issues/PRs**: Reference related issues
   - "Fixed pagination for large transcripts (#110)"

6. **Group Related Changes**: Don't scatter related items

7. **Highlight Breaking Changes**: Make them impossible to miss

8. **Include Upgrade Instructions**: Every release needs clear update steps

### Templates

#### Feature Release Template
```markdown
## vX.Y.Z - Feature Title

We're excited to announce vX.Y.Z with [major feature]!

### Highlights
- **New Feature**: Brief description of main feature
- **Improvement**: What got better
- **Performance**: Any speed/efficiency gains

### What's New

#### Major Feature Name
Detailed description of the feature, what it does, and how users benefit.

#### Other Additions
- Addition 1
- Addition 2

### Improvements
- Improvement with context

### Bug Fixes
- Fixed [issue] (#number)

### How to Update
...

### Contributors
Special thanks to @username for their contributions!
```

#### Security Release Template
```markdown
## vX.Y.Z - Security Patch

This release addresses security vulnerabilities. **All users should update immediately.**

### Security Fixes

| CVE | Severity | Component | Status |
|-----|----------|-----------|--------|
| CVE-XXXX-XXXXX | CRITICAL | package | ✅ Fixed |

### What's Fixed
- Detailed description of security fix

### Affected Versions
- Versions X.Y.Z and earlier are affected

### How to Update
...

### Reporting Security Issues
Report vulnerabilities via [security advisory page].
```

#### Patch Release Template
```markdown
## vX.Y.Z - Bug Fix Release

This release includes bug fixes and minor improvements.

### Bug Fixes
- Fixed [specific issue] (#number)
- Fixed [another issue]

### Improvements
- Minor improvement

### How to Update
...
```

### GitHub Release vs CHANGELOG

| Aspect | GitHub Release | CHANGELOG.md |
|--------|---------------|--------------|
| Audience | Users browsing GitHub | Developers, detailed reference |
| Length | Concise, highlights | Comprehensive |
| Format | Markdown with images OK | Plain markdown |
| Sections | Highlights, How to Update | Full categorized list |
| Links | Issue/PR numbers | Full URLs |

### What NOT to Include

- Internal implementation details users don't care about
- Every single commit message
- Placeholder text or TODOs
- Sensitive information (credentials, internal URLs)
- Blame or negative comments about past code

---

## Semantic Versioning Guide

OpenTranscribe follows [Semantic Versioning](https://semver.org/):

- **MAJOR (X.0.0)**: Breaking changes, major rewrites
- **MINOR (0.X.0)**: New features, backwards compatible
- **PATCH (0.0.X)**: Bug fixes, security patches

### When to Increment

| Change Type | Version Part | Example |
|-------------|--------------|---------|
| Breaking API change | MAJOR | 1.0.0 → 2.0.0 |
| New feature | MINOR | 0.2.0 → 0.3.0 |
| Bug fix | PATCH | 0.2.0 → 0.2.1 |
| Security fix | PATCH | 0.2.0 → 0.2.1 |
| Performance improvement | MINOR or PATCH | Depends on scope |
| Documentation only | No version change | - |

---

## Related Documentation

- [CONTRIBUTING.md](./CONTRIBUTING.md) - How to contribute
- [SECURITY.md](./SECURITY.md) - Security policy
- [BUILD_PIPELINE.md](./BUILD_PIPELINE.md) - CI/CD details
- [DOCKER_DEPLOYMENT.md](./DOCKER_DEPLOYMENT.md) - Deployment guide
- [SECURITY_SCANNING.md](./SECURITY_SCANNING.md) - Security scan details
