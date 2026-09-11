import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

type BeforeNavigateCallback = (navigation: { type: string; cancel: () => void }) => void;

const registered: BeforeNavigateCallback[] = [];

vi.mock('$app/navigation', () => ({
  beforeNavigate: (fn: BeforeNavigateCallback) => {
    registered.push(fn);
  },
}));

import { guardUnsavedChanges } from './unsavedChangesGuard';

/** Drive the callback the guard registered, as SvelteKit's router would. */
function navigate(type: string): { cancelled: boolean } {
  const result = { cancelled: false };
  for (const fn of registered) {
    fn({
      type,
      cancel: () => {
        result.cancelled = true;
      },
    });
  }
  return result;
}

let confirmSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  registered.length = 0;
  confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  confirmSpy.mockRestore();
});

describe('guardUnsavedChanges (issue #787)', () => {
  it('lets a clean page navigate without asking anything', () => {
    guardUnsavedChanges(
      () => false,
      () => 'Discard?'
    );

    const { cancelled } = navigate('link');

    expect(cancelled).toBe(false);
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('prompts on in-app navigation when there are unsaved edits', () => {
    guardUnsavedChanges(
      () => true,
      () => 'Discard your speaker names?'
    );

    navigate('link');

    expect(window.confirm).toHaveBeenCalledWith('Discard your speaker names?');
  });

  it('cancels the navigation when the user declines', () => {
    confirmSpy.mockReturnValue(false);
    guardUnsavedChanges(
      () => true,
      () => 'Discard?'
    );

    const { cancelled } = navigate('link');

    expect(cancelled).toBe(true);
  });

  it('lets the navigation through when the user accepts', () => {
    confirmSpy.mockReturnValue(true);
    guardUnsavedChanges(
      () => true,
      () => 'Discard?'
    );

    const { cancelled } = navigate('link');

    expect(cancelled).toBe(false);
  });

  it('re-reads the dirty state on every navigation, so saving clears the guard', () => {
    let dirty = true;
    guardUnsavedChanges(
      () => dirty,
      () => 'Discard?'
    );

    navigate('link');
    expect(window.confirm).toHaveBeenCalledTimes(1);

    dirty = false; // the user pressed Save
    const { cancelled } = navigate('link');

    expect(window.confirm).toHaveBeenCalledTimes(1);
    expect(cancelled).toBe(false);
  });

  it('cancels a tab-close WITHOUT calling confirm()', () => {
    // SvelteKit's own `beforeunload` listener (client.js `_start_router`) invokes
    // every beforeNavigate callback with type 'leave' and turns `cancel()` into
    // `preventDefault()` + `returnValue`, i.e. the browser's own dialog. Calling
    // window.confirm() there would be a second prompt the browser ignores anyway.
    guardUnsavedChanges(
      () => true,
      () => 'Discard?'
    );

    const { cancelled } = navigate('leave');

    expect(cancelled).toBe(true);
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('does not block a tab-close on a clean page', () => {
    guardUnsavedChanges(
      () => false,
      () => 'Discard?'
    );

    const { cancelled } = navigate('leave');

    expect(cancelled).toBe(false);
  });

  it('registers exactly one callback', () => {
    guardUnsavedChanges(
      () => false,
      () => 'Discard?'
    );

    expect(registered).toHaveLength(1);
  });
});
