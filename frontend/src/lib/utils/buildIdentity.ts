/**
 * Build-identity helpers for the About dialog (issue #1273).
 *
 * The bundle running in a tab and the backend serving it can each identify themselves by a
 * release version, a commit, or both, and the two sides do not use the same spelling:
 *
 *   release image   version `v0.6.0`   commit `abc1234`
 *   dev image       version `dev-abc1234`   commit `abc1234` (or `unknown`)
 *   local dev stack commit `abc1234-dirty` (opentr.sh marks an unclean tree)
 *   vite dev tab    version `0.5.1`   commit `unknown`
 *
 * A `dev-<sha>` version can never equal a release semver, so comparing versions alone cried
 * "stale build" on every dev image. The rule is: compare commits when BOTH sides know one,
 * else compare release versions when both are releases, else say nothing. A false warning is
 * worse than no warning.
 */

export interface BuildIdentity {
  version?: string | null;
  commit?: string | null;
}

export type BuildComparison = 'match' | 'mismatch' | 'unknown';

export type ParsedVersion =
  | { kind: 'release'; semver: string }
  | { kind: 'dev'; commit: string }
  | { kind: 'unknown' };

export type SourceRef =
  | { kind: 'commit'; sha: string; exact: boolean }
  | { kind: 'release'; tag: string }
  | { kind: 'none' };

const RELEASE_RE = /^v?(\d+\.\d+\.\d+)$/i;
const DEV_RE = /^dev-([0-9a-f]{7,40})$/i;
const COMMIT_RE = /^([0-9a-f]{7,40})(-dirty)?$/i;

/** Classify a version string: a release (`v0.6.0` / `0.6.0`), a dev image, or unusable. */
export function parseBuildVersion(v: string | null | undefined): ParsedVersion {
  const text = (v ?? '').trim();
  const release = RELEASE_RE.exec(text);
  if (release) return { kind: 'release', semver: release[1] };
  const dev = DEV_RE.exec(text);
  if (dev) return { kind: 'dev', commit: dev[1].toLowerCase() };
  return { kind: 'unknown' };
}

/**
 * Lowercase a commit and strip the `-dirty` marker `opentr.sh` appends for an unclean
 * working tree. Returns null for `unknown`, empty, or anything that is not 7-40 hex digits.
 */
export function normalizeCommit(
  c: string | null | undefined
): { sha: string; dirty: boolean } | null {
  const match = COMMIT_RE.exec((c ?? '').trim());
  if (!match) return null;
  return { sha: match[1].toLowerCase(), dirty: Boolean(match[2]) };
}

/** The explicit commit if there is a usable one, else the commit inside a `dev-<sha>` version. */
function effectiveCommit(id: BuildIdentity): { sha: string; dirty: boolean } | null {
  const explicit = normalizeCommit(id.commit);
  if (explicit) return explicit;
  const parsed = parseBuildVersion(id.version);
  return parsed.kind === 'dev' ? { sha: parsed.commit, dirty: false } : null;
}

/** Short and full SHAs of the same commit share a prefix. */
function sameCommit(a: string, b: string): boolean {
  return a.startsWith(b) || b.startsWith(a);
}

/** Is the running bundle (`client`) the same build as the server it talks to? */
export function compareBuilds(client: BuildIdentity, server: BuildIdentity): BuildComparison {
  const clientCommit = effectiveCommit(client);
  const serverCommit = effectiveCommit(server);
  if (clientCommit && serverCommit) {
    return sameCommit(clientCommit.sha, serverCommit.sha) ? 'match' : 'mismatch';
  }

  const clientVersion = parseBuildVersion(client.version);
  const serverVersion = parseBuildVersion(server.version);
  if (clientVersion.kind === 'release' && serverVersion.kind === 'release') {
    return clientVersion.semver === serverVersion.semver ? 'match' : 'mismatch';
  }
  return 'unknown';
}

/**
 * Where the AGPL section 13 source offer should point for the RUNNING server. A commit beats
 * a release tag. A dirty tree is not attributable to a commit, so its link is not "exact".
 * Never produces a `vdev-<sha>` tag, which does not exist.
 */
export function sourceRefFor(server: BuildIdentity): SourceRef {
  const commit = effectiveCommit(server);
  if (commit) return { kind: 'commit', sha: commit.sha, exact: !commit.dirty };
  const parsed = parseBuildVersion(server.version);
  if (parsed.kind === 'release') return { kind: 'release', tag: `v${parsed.semver}` };
  return { kind: 'none' };
}
