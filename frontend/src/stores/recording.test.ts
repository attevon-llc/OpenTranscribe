/**
 * Tests for `$stores/recording` — narrowly scoped to the dynamic `import('./toast')` at
 * module top-level.
 *
 * DEFECT THIS CATCHES: the original code wrapped `import('./toast'))` in a
 * `try { import('./toast').then(...) } catch { ... }` with the comment "Toast store not
 * available - errors will only be in console". A dynamic `import()` returns a promise; it
 * essentially never throws SYNCHRONOUSLY, so that `try/catch` could never catch a real
 * load failure. If `./toast` genuinely failed to load, the `.then()` callback would never
 * run and the rejection would go unhandled instead of being swallowed as the comment
 * claimed. The fix chains `.catch(() => {})` onto the promise so a failed import is
 * actually swallowed.
 *
 * SCOPE NOTE: `vi.mock('./toast', () => Promise.reject(...))` was tried first to force a
 * genuine load failure through the real module graph, but Vitest's mocker itself wraps a
 * rejecting factory in its own internal "there was an error when mocking a module" promise
 * that surfaces as an unhandled rejection independent of whether the CONSUMING code (this
 * file's fix) attaches a `.catch()` — that artifact fires identically whether the fix is
 * present or reverted, so it cannot distinguish fixed from broken. Instead this file (a)
 * sanity-checks that loading the real, unmocked module never produces an unhandled
 * rejection, and (b) reproduces the exact old-vs-new promise chain shape in isolation
 * (no module mocking involved) to prove the fix's mechanism: the old bare
 * `try { p.then(...) } catch {}` shape leaves a rejected promise unhandled, and the new
 * `p.then(...).catch(() => {})` shape does not.
 *
 * This file does not attempt to cover the rest of `recording.ts` (the `MediaRecorder`/
 * `AudioContext` singleton) — that is out of scope for this bug fix.
 */

import { describe, it, expect, vi } from 'vitest';

describe('recording store — dynamic toast import', () => {
  it('loading the real module never produces an unhandled rejection', async () => {
    vi.resetModules();

    const rejections: unknown[] = [];
    const onUnhandledRejection = (reason: unknown) => rejections.push(reason);
    process.on('unhandledRejection', onUnhandledRejection);

    try {
      await import('./recording');
      // Give any stray rejection a turn of the event loop to surface.
      await new Promise((resolve) => setTimeout(resolve, 20));
    } finally {
      process.off('unhandledRejection', onUnhandledRejection);
    }

    expect(rejections).toHaveLength(0);
  });

  /** Simulates a failed dynamic import without invoking the real module loader. */
  function rejectingImporter(): Promise<{ toastStore: string }> {
    return Promise.reject(new Error('chunk load failed'));
  }

  async function withUnhandledRejectionTracking(
    run: () => void | Promise<void>
  ): Promise<unknown[]> {
    const rejections: unknown[] = [];
    const onUnhandledRejection = (reason: unknown) => rejections.push(reason);
    process.on('unhandledRejection', onUnhandledRejection);
    try {
      await run();
      await new Promise((resolve) => setTimeout(resolve, 0));
    } finally {
      process.off('unhandledRejection', onUnhandledRejection);
    }
    return rejections;
  }

  it('control: the OLD try/catch-around-.then() shape leaves the rejection unhandled', async () => {
    const rejections = await withUnhandledRejectionTracking(() => {
      let toastStore: string | null = null;
      try {
        rejectingImporter().then((module) => {
          toastStore = module.toastStore;
        });
      } catch {
        // A synchronous throw never happens here for an async rejection -
        // this catch cannot help, which is exactly the bug.
      }
      void toastStore;
    });

    expect(rejections).toHaveLength(1);
  });

  it('the NEW .catch()-chained shape swallows the rejection with no unhandled rejection', async () => {
    const rejections = await withUnhandledRejectionTracking(() => {
      let toastStore: string | null = null;
      rejectingImporter()
        .then((module) => {
          toastStore = module.toastStore;
        })
        .catch(() => {
          // Toast store not available - errors will only be in console
        });
      void toastStore;
    });

    expect(rejections).toHaveLength(0);
  });
});
