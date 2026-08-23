/**
 * COMPLETENESS meta-test for `$lib/session/clearUserState`.
 *
 * DEFECT THIS CATCHES: `clearUserState()` is documented as the single source of
 * truth for session teardown, but nothing enforced that. `apiCache.clear()` was
 * missing from it for the entire life of the module — User B, logging in in the
 * same tab, saw User A's file list, tags, collections, speakers and groups for
 * up to the 5-minute TTL — and `capabilities` was missing too, leaking one
 * cloud user's TIER-SCOPED capability map into the next session. Both survived
 * review because a missing registration looks like nothing at all.
 *
 * So this test does not hand-list the stores. It ENUMERATES the candidates from
 * source with four detectors (below) and fails when a candidate is neither
 * registered in `clearUserState.ts` nor exempted here with a written reason.
 * Add a store with module-level user state and this test goes red on the commit
 * that adds it.
 *
 * The exemption list is checked in BOTH directions: an exemption whose module no
 * longer matches any detector is itself a failure, because a stale exemption is
 * how this kind of list rots into a rubber stamp.
 */

import { describe, it, expect } from 'vitest';
import { readdirSync, statSync, readFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';

const SRC = resolve(__dirname, '../..');
const CLEAR_USER_STATE = join(SRC, 'lib/session/clearUserState.ts');

/** Directories that can hold module-level session state. */
const SCAN_ROOTS = ['stores', 'lib'];

/**
 * Modules that match a detector but hold NO user-session state.
 *
 * Every entry needs a reason, and the reason must say *why the previous user's
 * data cannot survive here* — "not important" is not a reason. Keys are
 * `src`-relative, extension-less.
 */
const EXEMPT: Record<string, string> = {
  // ── Reset by a different, deliberate mechanism in logout() ──
  'stores/auth':
    'authStore.reset() is called explicitly by logout() and every failed-login path; ' +
    'clearUserState must NOT reset auth or it would race the caller.',
  'lib/axios':
    'sessionAbortController / isRefreshing / refreshQueue are reset by abortAllRequests(), ' +
    'which logout() calls before clearUserState() precisely so in-flight responses cannot ' +
    'repopulate a store afterwards.',
  'lib/prefetch':
    'inflight / failedCache only hold file UUIDs and timestamps, and every prefetch request ' +
    'goes through axiosInstance without its own signal, so abortAllRequests() cancels them ' +
    'before clearUserState() runs. No response body is retained here.',

  'lib/chat/revealPacer':
    'RevealPacer is a CLASS with instance state only — there is no module-level ' +
    'instance. Each ChatTracePanel constructs its own and it is discarded with the ' +
    'component, so no previous user’s data can survive here. The detector matched ' +
    'on the `reset()` method name, not on real module state. It also holds only ' +
    'node keys and reveal timings, never trace content.',

  // ── Covered transitively by a registered module ──
  'lib/services/uploadService':
    'uploadService.reset() is called by uploadsStore.reset(), which IS registered.',

  // ── Server-side reset endpoints, not module state ──
  'lib/api/downloadSettings': 'resetDownloadSettings() is an HTTP DELETE, not module state.',
  'lib/api/organizationContext': 'resetOrganizationContext() is an HTTP DELETE, not module state.',
  'lib/api/redactionSettings': 'resetRedactionSettings() is an HTTP DELETE, not module state.',
  'lib/api/speakerAttributeSettings':
    'resetSpeakerAttributeSettings() is an HTTP DELETE, not module state.',
  'lib/api/transcriptionSettings':
    'resetTranscriptionSettings() is an HTTP DELETE, not module state.',

  // ── Per-instance state created by a factory, not module-level ──
  'lib/search/findInText':
    'occurrences/current live inside the controller each caller constructs; they die with the ' +
    'component, not with the module.',
  'lib/api/chatStream':
    'resetWatchdog() is a local closure inside a single stream call; the stream itself is ' +
    'aborted by chatStore.reset(), which IS registered.',
  'lib/services/stallWatchdog': 'Timer handles only, owned by the caller that started them.',

  // ── Deployment/system state, identical for every user ──
  'lib/services/llmService':
    'statusCache holds the deployment-wide LLM availability probe (/llm/status), which is not ' +
    'user-scoped; re-fetching per session would be a needless request.',
  'lib/i18n/index':
    'loadedLanguages / inFlightLoads hold locale CHUNKS. Locale is a preserved preference, and ' +
    'a translation bundle contains no user data.',
  'lib/monitoring':
    'initialized is a one-shot bootstrap flag; re-running init on logout would double-register ' +
    'error handlers.',
  'lib/utils/chatMarkdown':
    'hookInstalled tracks a process-global DOMPurify hook. Removing it on logout would leave ' +
    'the next session sanitising with a weaker profile.',

  // ── Pure UI state with no user data ──
  'lib/scrollLock': 'A nesting counter for overflow:hidden. Reset per navigation by afterNavigate.',
  'stores/toast': 'toastCounter is a monotonic id source; toastStore.clear() IS registered.',
  'stores/recording':
    'The module-level `toastStore` binding is an imported reference, not state; the recording ' +
    'blob and tracks ARE cleared via recordingManager, which is registered.',

  // ── Self ──
  'lib/session/clearUserState':
    'The module under test. It holds no state of its own — it only orchestrates the others.',
};

// ─────────────────────────────────────────────────────────────────────────────
// Mechanical enumeration
// ─────────────────────────────────────────────────────────────────────────────

function walk(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    if (statSync(p).isDirectory()) {
      walk(p, acc);
    } else if (/\.(ts|js)$/.test(p) && !/\.(test|spec)\.(ts|js)$/.test(p) && !/\.d\.ts$/.test(p)) {
      acc.push(p);
    }
  }
  return acc;
}

/** `src`-relative, extension-less module id: `src/stores/toast.ts` → `stores/toast`. */
function moduleId(absPath: string): string {
  return relative(SRC, absPath)
    .replace(/\\/g, '/')
    .replace(/\.(ts|js)$/, '');
}

interface Candidate {
  id: string;
  signals: string[];
}

/**
 * Four detectors, in the order the review specified. Each answers "could the
 * previous user's data still be sitting in this module after logout?".
 */
function detect(source: string): string[] {
  const signals: string[] = [];

  // 1. Exported top-level clear*/reset* function — a module announcing it holds
  //    resettable state.
  const exportedResets = [
    ...source.matchAll(/export\s+(?:async\s+)?function\s+((?:clear|reset)[A-Za-z0-9_]*)/g),
  ].map((m) => m[1]);
  if (exportedResets.length) signals.push(`exported-fn:${[...new Set(exportedResets)].join(',')}`);

  // 2. A store-API surface member named clear*/reset* (object-literal method or
  //    shorthand). This is how every `create*Store()` factory exposes teardown.
  const apiMembers = [...source.matchAll(/^\s{2,6}((?:reset|clear)[A-Za-z0-9_]*)\s*[(:,]/gm)].map(
    (m) => m[1]
  );
  if (apiMembers.length) signals.push(`store-api:${[...new Set(apiMembers)].join(',')}`);

  // 3. Module-level Map/Set/WeakMap bound to a lowercase name — a cache. An
  //    UPPER_SNAKE name is a constant lookup table, not state.
  const caches = [
    ...source.matchAll(
      /^(?:const|let)\s+([a-z][A-Za-z0-9_$]*)(?::[^=]+)?\s*=\s*new\s+(?:Map|Set|WeakMap)/gm
    ),
  ].map((m) => m[1]);
  if (caches.length) signals.push(`module-cache:${[...new Set(caches)].join(',')}`);

  // 4. Module-level `let` — mutable state that outlives every component.
  const lets = [...source.matchAll(/^let\s+([A-Za-z0-9_$]+)/gm)].map((m) => m[1]);
  if (lets.length) signals.push(`module-let:${[...new Set(lets)].join(',')}`);

  return signals;
}

function enumerateCandidates(): Candidate[] {
  const files = SCAN_ROOTS.flatMap((root) => walk(join(SRC, root)));
  const candidates: Candidate[] = [];
  for (const file of files.sort()) {
    const signals = detect(readFileSync(file, 'utf8'));
    if (signals.length) candidates.push({ id: moduleId(file), signals });
  }
  return candidates;
}

/** Module ids that `clearUserState.ts` dynamically imports. */
function registeredModules(): Set<string> {
  const source = readFileSync(CLEAR_USER_STATE, 'utf8');
  const specs = [...source.matchAll(/import\(\s*['"]([^'"]+)['"]\s*\)/g)].map((m) => m[1]);
  return new Set(specs.map((s) => s.replace(/^\$stores\//, 'stores/').replace(/^\$lib\//, 'lib/')));
}

// ─────────────────────────────────────────────────────────────────────────────

describe('clearUserState completeness', () => {
  const candidates = enumerateCandidates();
  const registered = registeredModules();

  it('finds candidate modules at all (a broken detector would report a clean sweep)', () => {
    // Calibration guard, in the spirit of the auditors' --selftest: if the
    // detectors stopped matching, every assertion below would pass vacuously.
    expect(candidates.length).toBeGreaterThan(25);
    expect(registered.size).toBeGreaterThan(15);
  });

  it('registers the two stores whose omission caused the known leaks', () => {
    // apiCache: User B saw User A's file list/tags/collections for the 5 min TTL.
    expect(registered.has('lib/apiCache')).toBe(true);
    // capabilities: cloud User B inherited User A's tier-scoped surface map.
    expect(registered.has('stores/capabilities')).toBe(true);
  });

  it('every module holding module-level state is registered or exempted with a reason', () => {
    const unaccounted = candidates
      .filter((c) => !registered.has(c.id) && !(c.id in EXEMPT))
      .map((c) => `${c.id}  [${c.signals.join(' | ')}]`);

    expect(
      unaccounted,
      'These modules hold module-level state but are not cleared on session change. ' +
        'Register them in src/lib/session/clearUserState.ts, or add an entry to EXEMPT in ' +
        'this file explaining why the previous user’s data cannot survive there.'
    ).toEqual([]);
  });

  it('has no stale exemptions (an exemption for a module that no longer matches)', () => {
    const candidateIds = new Set(candidates.map((c) => c.id));
    const stale = Object.keys(EXEMPT).filter((id) => !candidateIds.has(id));
    expect(
      stale,
      'These EXEMPT entries no longer match any detector — the module was deleted, renamed, or ' +
        'its state removed. Delete the entry so the list keeps meaning something.'
    ).toEqual([]);
  });

  it('every exemption states a reason', () => {
    const empty = Object.entries(EXEMPT)
      .filter(([, reason]) => reason.trim().length < 30)
      .map(([id]) => id);
    expect(empty).toEqual([]);
  });

  it('does not clear preserved user PREFERENCES', () => {
    // The other half of the contract: over-clearing is a bug too. Theme, locale
    // and view-mode survive logout by design (documented in the module header).
    const source = readFileSync(CLEAR_USER_STATE, 'utf8');
    const removed = [...source.matchAll(/^\s*'([^']+)', \/\//gm)].map((m) => m[1]);
    expect(removed).not.toContain('theme');
    expect(removed).not.toContain('locale');
    expect(removed).not.toContain('galleryViewMode');
  });
});
