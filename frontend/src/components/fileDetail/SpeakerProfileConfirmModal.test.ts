import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

const mockScroll = vi.hoisted(() => ({ lockScroll: vi.fn(), unlockScroll: vi.fn() }));
vi.mock('$lib/scrollLock', () => mockScroll);

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import SpeakerProfileConfirmModal from './SpeakerProfileConfirmModal.svelte';

beforeEach(() => vi.clearAllMocks());

describe('SpeakerProfileConfirmModal', () => {
  it('holds one more scroll lock than BaseModal for exactly its lifetime', () => {
    const { unmount } = render(SpeakerProfileConfirmModal, { props: { title: 'T', message: 'M' } });
    // BaseModal takes one for an open dialog; this component takes its own on top
    // (the page used to take it). Every lock is released on unmount.
    const locks = mockScroll.lockScroll.mock.calls.length;
    expect(locks).toBe(2);
    expect(mockScroll.unlockScroll).not.toHaveBeenCalled();

    unmount();

    expect(mockScroll.unlockScroll).toHaveBeenCalledTimes(locks);
  });

  it('reports the chosen action', async () => {
    const seen: string[] = [];
    const record = (name: string) => () => seen.push(name);
    const { getByText } = render(SpeakerProfileConfirmModal, {
      props: { title: 'T', message: 'M' },
      events: {
        updateProfile: record('updateProfile'),
        createNewProfile: record('createNewProfile'),
        cancel: record('cancel'),
      },
    });

    await fireEvent.click(getByText('speakerProfile.updateGlobally'));
    await fireEvent.click(getByText('speakerProfile.createNew'));
    await fireEvent.click(getByText('common.cancel'));

    expect(seen).toEqual(['updateProfile', 'createNewProfile', 'cancel']);
  });
});
