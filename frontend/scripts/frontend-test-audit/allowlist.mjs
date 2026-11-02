/** The reviewed-exemption allowlist: parsing, reason validation and count-aware matching. */

// ------------------------------------------------------------------------------ allowlist

/** Reason prefix marking an entry as DEFERRED WORK rather than an accepted pattern. */
export const BACKLOG_PREFIX = 'BACKLOG';

/**
 * Reasons that read as "no justification" rather than a real, written one. Exact match only
 * (case-insensitive, trimmed) — a reason that merely CONTAINS one of these words as part of an
 * actual sentence is not rejected; only a line whose entire reason IS one of these placeholders
 * is. `no reason given` is the sentinel this file used to silently substitute for an empty
 * reason (issue #826) — included here so a reason that is literally that sentinel is *also*
 * rejected, not just an empty one. The allowlist's own header already names `flaky`/`TODO` as
 * non-reasons; this is what makes that documentation actually enforced.
 */
const NOT_A_REAL_REASON = new Set([
  'no reason given',
  'todo',
  'tbd',
  'flaky',
  'wip',
  'fixme',
  'n/a',
  'na',
  'none',
]);

function isRealReason(reason) {
  return reason.length > 0 && !NOT_A_REAL_REASON.has(reason.toLowerCase());
}

/** Thrown when an allowlist entry's reason is empty or a placeholder (issue #826). */
export class AllowlistReasonError extends Error {}

/**
 * `key -> [reason, ...]`, one entry per LINE. An array, not a string: one line buys one
 * finding, so duplicate keys encode a count rather than being an error to reject.
 *
 * REJECTS (throws `AllowlistReasonError`) an entry whose reason is empty or a placeholder,
 * rather than silently substituting `'no reason given'` for it — the substitution was the
 * whole finding in issue #826: a mandatory-reason rule that was never actually checked.
 */
export function loadAllowlist(text) {
  const entries = new Map();
  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (!line || line.startsWith('#')) continue;
    const hash = line.indexOf('#');
    const key = (hash === -1 ? line : line.slice(0, hash)).trim();
    const reason = hash === -1 ? '' : line.slice(hash + 1).trim();
    if (!key) continue;
    if (!isRealReason(reason)) {
      throw new AllowlistReasonError(
        `line ${i + 1}: allowlist entry for \`${key}\` has no real reason ` +
          `(got ${JSON.stringify(reason)}). A written reason is mandatory — see the header ` +
          'of test-audit-allowlist.txt.'
      );
    }
    if (!entries.has(key)) entries.set(key, []);
    entries.get(key).push(reason);
  }
  return entries;
}

const keyOf = (f) => `${f.path}::${f.test}::${f.category}`;

/**
 * Split findings by allowlist coverage, pairing one line to one finding.
 *
 * Returns `{ open, backlog, accepted, stale }`. `stale` names every key holding more lines
 * than the tree still produces — the key vanished entirely, or it was PARTIALLY fixed. The
 * second case is the one a set difference is blind to, and it is the common one: a test with
 * three offending assertions gets three lines, and repairing two of them must not leave the
 * third quietly exempt.
 */
export function applyAllowlist(findings, allowed) {
  const byKey = new Map();
  for (const f of findings) {
    const key = keyOf(f);
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(f);
  }

  const open = [];
  const backlog = [];
  let accepted = 0;
  for (const [key, group] of byKey) {
    const reasons = allowed.get(key) ?? [];
    group.slice(0, reasons.length).forEach((f, i) => {
      if (reasons[i].startsWith(BACKLOG_PREFIX)) backlog.push(f);
      else accepted += 1;
    });
    open.push(...group.slice(reasons.length));
  }

  const stale = [];
  for (const [key, reasons] of [...allowed].sort(([a], [b]) => (a < b ? -1 : 1))) {
    const live = byKey.get(key)?.length ?? 0;
    if (live >= reasons.length) continue;
    stale.push(
      live === 0
        ? `${key}  (${reasons.length} entr${reasons.length === 1 ? 'y' : 'ies'}, 0 findings)`
        : `${key}  (${reasons.length} entries, ${live} finding(s) — delete ${
            reasons.length - live
          })`
    );
  }
  return { open, backlog, accepted, stale };
}
