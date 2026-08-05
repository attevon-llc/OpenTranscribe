/**
 * The watchdog replaced a 5-minute total-request timeout that failed every
 * upload slower than ~50 MB/s against a 15 GB limit. These tests pin the
 * distinction that made the old control wrong: slow must not abort, silent must.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createStallWatchdog } from './stallWatchdog';

describe('createStallWatchdog', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not abort a slow-but-moving transfer, however long it runs', () => {
    const watchdog = createStallWatchdog({ stallTimeoutMs: 1000 });

    // 60 ticks of 1 byte each, 900 ms apart: 54 s total — an order of magnitude
    // past the stall window, but never idle for a full window.
    for (let i = 1; i <= 60; i++) {
      vi.advanceTimersByTime(900);
      watchdog.notifyProgress(i, 10_000);
    }

    expect(watchdog.signal.aborted).toBe(false);
    expect(watchdog.stalled).toBe(false);
    watchdog.dispose();
  });

  it('aborts when no bytes move for the stall window', () => {
    const watchdog = createStallWatchdog({ stallTimeoutMs: 1000 });

    watchdog.notifyProgress(500, 10_000);
    vi.advanceTimersByTime(999);
    expect(watchdog.stalled).toBe(false);

    vi.advanceTimersByTime(1);
    expect(watchdog.stalled).toBe(true);
    expect(watchdog.signal.aborted).toBe(true);
    watchdog.dispose();
  });

  it('aborts a connection that never delivers a first byte', () => {
    const watchdog = createStallWatchdog({ stallTimeoutMs: 1000 });

    vi.advanceTimersByTime(1000);

    expect(watchdog.stalled).toBe(true);
    watchdog.dispose();
  });

  it('treats a repeated byte count as no progress', () => {
    const watchdog = createStallWatchdog({ stallTimeoutMs: 1000 });

    watchdog.notifyProgress(500, 10_000);
    vi.advanceTimersByTime(600);
    watchdog.notifyProgress(500, 10_000);
    vi.advanceTimersByTime(400);

    expect(watchdog.stalled).toBe(true);
    watchdog.dispose();
  });

  it('switches to the longer ceiling once the body is fully sent', () => {
    // Progress events stop firing at 100%, so the transfer window must not apply
    // while the object store writes and checksums a multi-GB upload.
    const watchdog = createStallWatchdog({ stallTimeoutMs: 1000, finalizeTimeoutMs: 10_000 });

    watchdog.notifyProgress(10_000, 10_000);
    vi.advanceTimersByTime(9999);
    expect(watchdog.stalled).toBe(false);

    vi.advanceTimersByTime(1);
    expect(watchdog.stalled).toBe(true);
    watchdog.dispose();
  });

  it('does not re-arm the finalize window on repeated completion ticks', () => {
    const watchdog = createStallWatchdog({ stallTimeoutMs: 1000, finalizeTimeoutMs: 10_000 });

    watchdog.notifyProgress(10_000, 10_000);
    vi.advanceTimersByTime(9000);
    watchdog.notifyProgress(10_000, 10_000);
    vi.advanceTimersByTime(1000);

    expect(watchdog.stalled).toBe(true);
    watchdog.dispose();
  });

  it('stops firing after dispose', () => {
    const watchdog = createStallWatchdog({ stallTimeoutMs: 1000 });

    watchdog.dispose();
    vi.advanceTimersByTime(60_000);

    expect(watchdog.stalled).toBe(false);
    expect(watchdog.signal.aborted).toBe(false);
  });
});
