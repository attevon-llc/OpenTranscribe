/**
 * Issue #788 (last acceptance criterion): the toast's `action` slot (a "Retry"
 * button) must be disabled while `retryAfterSeconds` is outstanding and
 * silently re-enable once it elapses -- WITHOUT re-announcing to assistive
 * tech, which rules out a ticking/re-rendered text node. These tests pin the
 * disabled -> enabled transition, the timer cleanup on unmount, and the
 * degrade-cleanly path when there is nothing to count down.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, opts?: Record<string, unknown>) => string) => void) => {
      run((key: string, opts?: Record<string, unknown>) =>
        opts && 'count' in opts ? `${key}:${opts.count}` : key
      );
      return () => {};
    },
  },
}));

import Toast from './Toast.svelte';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('Toast action (issue #788)', () => {
  it('disables the action until retryAfterSeconds elapses, then re-enables it', async () => {
    vi.useFakeTimers();
    const onClick = vi.fn();
    const { getByText } = render(Toast, {
      props: {
        message: 'Rate limited',
        type: 'warning',
        duration: 0,
        retryAfterSeconds: 30,
        action: { label: 'Retry', onClick },
      },
    });

    const button = getByText('Retry') as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    // Clicking while disabled must not fire the handler -- the browser
    // suppresses click on a disabled button, but assert the contract anyway.
    await fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();

    // Not yet elapsed: still disabled.
    vi.advanceTimersByTime(29_000);
    expect(button.disabled).toBe(true);

    // Elapsed: silently re-enabled, with no new text rendered anywhere in
    // the toast (the re-announce hazard this design avoids).
    const messageBefore = document.body.textContent;
    vi.advanceTimersByTime(1_000);
    await Promise.resolve();
    expect(button.disabled).toBe(false);
    expect(document.body.textContent).toBe(messageBefore);

    await fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('clears the re-enable timer on unmount (no leaked setTimeout)', () => {
    vi.useFakeTimers();
    const clearSpy = vi.spyOn(global, 'clearTimeout');
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const onClick = vi.fn();
    const { unmount } = render(Toast, {
      props: {
        message: 'Rate limited',
        type: 'warning',
        duration: 0,
        retryAfterSeconds: 10,
        action: { label: 'Retry', onClick },
      },
    });

    unmount();

    // The proof that matters: letting the original delay elapse AFTER unmount
    // must not throw (a leaked timer writing to a destroyed component's state)
    // and must not touch the now-unmounted action. `clearTimeout` having been
    // called is corroborating evidence, not the whole story.
    expect(() => vi.advanceTimersByTime(60_000)).not.toThrow();
    expect(errorSpy).not.toHaveBeenCalled();
    expect(onClick).not.toHaveBeenCalled();
    expect(clearSpy).toHaveBeenCalled();

    errorSpy.mockRestore();
  });

  it('starts the action enabled when retryAfterSeconds is absent (degradation path)', () => {
    const { getByText } = render(Toast, {
      props: {
        message: 'Something failed',
        type: 'error',
        duration: 0,
        action: { label: 'Retry', onClick: vi.fn() },
      },
    });

    const button = getByText('Retry') as HTMLButtonElement;
    expect(button.disabled).toBe(false);
  });
});
