/** In-memory fixtures the self-test feeds the detectors. */

import { AllowlistReasonError, applyAllowlist, loadAllowlist } from './allowlist.mjs';

// ------------------------------------------------------------------------------- self-test

/**
 * Each case is source that MUST produce the named category, plus `clean` cases that must
 * produce nothing. This is the auditor auditing itself: a detector that silently stops
 * matching (a refactor, a vitest API change) is indistinguishable from a clean suite, which
 * is exactly the failure mode this whole script exists to prevent.
 */
export const SELFTEST_CASES = [
  ['only-leak', "describe('d', () => { it.only('a', () => { expect(1).toBe(2); }); });"],
  ['skipped-test', "describe('d', () => { it.skip('a', () => { expect(1).toBe(2); }); });"],
  ['skipped-test', "describe('d', () => { it.todo('a'); });"],
  ['no-assertion', "describe('d', () => { it('a', () => { const x = compute(); }); });"],
  ['unfalsifiable', "describe('d', () => { it('a', () => { expect(true).toBe(true); }); });"],
  ['unfalsifiable', "describe('d', () => { it('a', () => { expect(1).toBe(1); }); });"],
  ['weak-only', "describe('d', () => { it('a', () => { expect(thing()).toBeDefined(); }); });"],
  ['weak-only', "describe('d', () => { it('a', () => { expect(() => f()).not.toThrow(); }); });"],
  [
    'conditional-only',
    "describe('d', () => { it('a', () => { if (feature) { expect(x).toBe(1); } }); });",
  ],
  [
    'conditional-skip',
    "describe('d', () => { it('a', (ctx) => { if (!feature) { ctx.skip(); } else { expect(x).toBe(1); } }); });",
  ],
  [
    'conditional-skip',
    "describe('d', () => { it('a', () => { if (!feature) return; expect(x).toBe(1); }); });",
  ],
  [
    'floating-async-assertion',
    "describe('d', () => { it('a', async () => { expect(p()).rejects.toThrow(); }); });",
  ],
  [
    'mock-heavy',
    `describe('d', () => { it('a', () => {
      const m = vi.fn(); vi.spyOn(o, 'x'); vi.stubGlobal('fetch', vi.fn());
      m.mockReturnValue(1); m.mockResolvedValue(2); m.mockImplementation(() => 3);
      m.mockClear(); m.mockReset(); m.mockRestore(); m.mockReturnValueOnce(4);
      m.mockResolvedValueOnce(5); m.mockRejectedValue(6); m.mockRejectedValueOnce(7);
      m.mockImplementationOnce(() => 8);
      expect(render(C).container.textContent).toBe('x');
    }); });`,
  ],
  [
    'mock-only',
    "describe('d', () => { it('a', () => { doThing(); expect(mockPost).toHaveBeenCalledWith('/x'); }); });",
  ],
];

/** Sources that must produce NO finding — the false-positive half of the calibration. */
export const SELFTEST_CLEAN = [
  // Trap 1: throwing Testing Library queries ARE assertions.
  "describe('d', () => { it('a', () => { render(C); screen.getByRole('button', { name: 'Save' }); }); });",
  // Trap 1: a local helper that wraps expect() is an assertion.
  `function expectRow(el, label) { expect(el.textContent).toBe(label); }
   describe('d', () => { it('a', () => { expectRow(render(C).container, 'x'); }); });`,
  // `expect.arrayContaining` is a matcher ARGUMENT, not an assertion head.
  "describe('d', () => { it('a', () => { expect(list).toEqual(expect.arrayContaining(['a'])); }); });",
  // An awaited rejects chain is fine.
  "describe('d', () => { it('a', async () => { await expect(p()).rejects.toThrow(); }); });",
  // A call assertion alongside a real output assertion is not mock-only.
  `describe('d', () => { it('a', () => {
     expect(mockPost).toHaveBeenCalledWith('/x'); expect(result.name).toBe('ops');
   }); });`,
  // Guarded assertion plus an unguarded one is not conditional-only.
  "describe('d', () => { it('a', () => { expect(x).toBe(1); if (y) { expect(y).toBe(2); } }); });",
  // `toThrow()` (positive) is a real failure-mode assertion, not weak.
  "describe('d', () => { it('a', () => { expect(() => f()).toThrow('boom'); }); });",
];

/**
 * The allowlist rails, self-tested for the same reason the detectors are: a stale check that
 * stops noticing reports nothing, which reads exactly like a clean allowlist. Each case names
 * what must happen and what must NOT.
 */
export const ALLOWLIST_CASES = [
  [
    'one line covers one finding',
    () => {
      const f = { path: 'a.test.ts', test: 'x', category: 'weak-only', line: 1, detail: '' };
      const r = applyAllowlist([f], loadAllowlist('a.test.ts::x::weak-only  # reason'));
      return r.open.length === 0 && r.accepted === 1 && r.stale.length === 0;
    },
  ],
  [
    'one line does NOT cover three findings',
    () => {
      const f = (line) => ({
        path: 'a.test.ts',
        test: 'x',
        category: 'weak-only',
        line,
        detail: '',
      });
      const r = applyAllowlist([f(1), f(2), f(3)], loadAllowlist('a.test.ts::x::weak-only  # r'));
      return r.open.length === 2;
    },
  ],
  [
    'an entry whose finding is gone is STALE',
    () => applyAllowlist([], loadAllowlist('a.test.ts::x::weak-only  # r')).stale.length === 1,
  ],
  [
    'a PARTIAL fix leaves surplus lines, and they are STALE',
    () => {
      const f = { path: 'a.test.ts', test: 'x', category: 'weak-only', line: 1, detail: '' };
      const list = loadAllowlist(
        'a.test.ts::x::weak-only  # r\na.test.ts::x::weak-only  # r\na.test.ts::x::weak-only  # r'
      );
      const r = applyAllowlist([f], list);
      return r.open.length === 0 && r.stale.length === 1 && r.stale[0].includes('delete 2');
    },
  ],
  [
    'a BACKLOG reason is counted as deferred work, not accepted',
    () => {
      const f = { path: 'a.test.ts', test: 'x', category: 'weak-only', line: 1, detail: '' };
      const r = applyAllowlist([f], loadAllowlist('a.test.ts::x::weak-only  # BACKLOG(#431): tbd'));
      return r.backlog.length === 1 && r.accepted === 0 && r.open.length === 0;
    },
  ],
  [
    'a comment line is not an entry',
    () => loadAllowlist('# a.test.ts::x::weak-only  # r\n\n').size === 0,
  ],
  // Issue #826: the rail must REJECT a reason-less entry, not default it to a placeholder.
  [
    'an entry with no reason at all is REJECTED, not defaulted to a placeholder',
    () => {
      try {
        loadAllowlist('a.test.ts::x::weak-only');
        return false;
      } catch (err) {
        return err instanceof AllowlistReasonError;
      }
    },
  ],
  [
    'an entry with an empty reason after "#" is REJECTED',
    () => {
      try {
        loadAllowlist('a.test.ts::x::weak-only  #');
        return false;
      } catch (err) {
        return err instanceof AllowlistReasonError;
      }
    },
  ],
  [
    'a placeholder reason ("flaky") is REJECTED',
    () => {
      try {
        loadAllowlist('a.test.ts::x::weak-only  # flaky');
        return false;
      } catch (err) {
        return err instanceof AllowlistReasonError;
      }
    },
  ],
  [
    'a placeholder reason ("TODO") is REJECTED, case-insensitively',
    () => {
      try {
        loadAllowlist('a.test.ts::x::weak-only  # TODO');
        return false;
      } catch (err) {
        return err instanceof AllowlistReasonError;
      }
    },
  ],
  [
    'a genuine written reason is ACCEPTED',
    () => {
      const list = loadAllowlist('a.test.ts::x::weak-only  # a real, written reason');
      return (
        list.size === 1 &&
        JSON.stringify(list.get('a.test.ts::x::weak-only')) ===
          JSON.stringify(['a real, written reason'])
      );
    },
  ],
];
