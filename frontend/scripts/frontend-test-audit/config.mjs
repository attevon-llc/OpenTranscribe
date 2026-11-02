/** Paths and vocabulary shared by every detector of the frontend test auditor. */

import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
export const frontendRoot = resolve(here, '..', '..');

export const ALLOWLIST_PATH = join(frontendRoot, 'test-audit-allowlist.txt');

/** `vi.*` / `mock*` references above this in one test mean the test is mostly scaffolding. */
export const MAX_MOCK_REFS = 14;

export const CATEGORIES = [
  'only-leak',
  'skipped-test',
  'no-assertion',
  'unfalsifiable',
  'weak-only',
  'conditional-only',
  'conditional-skip',
  'floating-async-assertion',
  'mock-heavy',
  'mock-only',
];

export const SUITE_NAMES = new Set(['describe', 'suite']);
export const TEST_NAMES = new Set(['it', 'test', 'bench']);

/** Matchers that pass on almost any value. Weak as a test's SOLE assertion. */
export const WEAK_MATCHERS = new Set([
  'toBeDefined',
  'toBeTruthy',
  'toBeFalsy',
  'toBeNull',
  'toBeUndefined',
  'toThrow',
  'toThrowError',
]);

/** Matchers that only prove a mock was invoked. */
export const CALL_MATCHERS = new Set([
  'toHaveBeenCalled',
  'toHaveBeenCalledOnce',
  'toHaveBeenCalledTimes',
  'toHaveBeenCalledWith',
  'toHaveBeenLastCalledWith',
  'toHaveBeenNthCalledWith',
  'toHaveBeenCalledExactlyOnceWith',
  'toHaveReturned',
  'toHaveReturnedTimes',
]);

export const MOCK_HELPERS = new Set([
  'fn',
  'mock',
  'spyOn',
  'doMock',
  'mocked',
  'stubGlobal',
  'mockReturnValue',
  'mockReturnValueOnce',
  'mockResolvedValue',
  'mockResolvedValueOnce',
  'mockRejectedValue',
  'mockRejectedValueOnce',
  'mockImplementation',
  'mockImplementationOnce',
  'mockClear',
  'mockReset',
  'mockRestore',
]);

/**
 * Testing Library queries that throw on failure, so they assert. `queryBy*` deliberately
 * does NOT throw — it returns null — and is excluded.
 */
export const THROWING_QUERY = /^(get|find)(All)?By[A-Z]/;

/**
 * `expect.arrayContaining(...)` is an ARGUMENT to a matcher, not an assertion head. Treat it
 * as one and the parent-climb walks out of the real chain and reports a nonsense matcher
 * (calibration bug: `expect(titles).toEqual(expect.arrayContaining([...]))` was reported as
 * `expect([...]).null()` and counted as unfalsifiable because its args are all literals).
 * `expect.soft(x)` IS a head, so it is deliberately absent from this set.
 */
export const EXPECT_UTILITIES = new Set([
  'arrayContaining',
  'objectContaining',
  'stringContaining',
  'stringMatching',
  'closeTo',
  'any',
  'anything',
  'assertions',
  'hasAssertions',
  'unreachable',
  'extend',
  'addSnapshotSerializer',
]);
