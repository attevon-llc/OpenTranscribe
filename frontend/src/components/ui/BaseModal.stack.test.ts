/**
 * Escape and full-screen behaviour of BaseModal.
 *
 * DEFECT THIS CATCHES: every BaseModal listened for Escape on `window`, so with a dialog
 * opened from another (the legal reader over the first-run wizard) one key press closed
 * both, and a closed modal still called its `onClose`. Only the topmost open dialog may
 * answer Escape.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import BaseModal from './BaseModal.svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const escape = () => fireEvent.keyDown(window, { key: 'Escape' });

describe('BaseModal Escape with stacked dialogs', () => {
  it('closes only the topmost open modal', async () => {
    const closeLower = vi.fn();
    const closeUpper = vi.fn();
    render(BaseModal, { props: { isOpen: true, title: 'Lower', onClose: closeLower } });
    render(BaseModal, { props: { isOpen: true, title: 'Upper', onClose: closeUpper } });

    await escape();

    expect(closeUpper).toHaveBeenCalledTimes(1);
    expect(closeLower).not.toHaveBeenCalled();
  });

  it('hands Escape back to the lower modal once the upper one has closed', async () => {
    const closeLower = vi.fn();
    const closeUpper = vi.fn();
    render(BaseModal, { props: { isOpen: true, title: 'Lower', onClose: closeLower } });
    const upper = render(BaseModal, {
      props: { isOpen: true, title: 'Upper', onClose: closeUpper },
    });

    await upper.rerender({ isOpen: false });
    await escape();

    expect(closeLower).toHaveBeenCalledTimes(1);
    expect(closeUpper).not.toHaveBeenCalled();
  });

  it('a closed modal never answers Escape', async () => {
    const onClose = vi.fn();
    render(BaseModal, { props: { isOpen: false, title: 'Closed', onClose } });

    await escape();

    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('BaseModal fullscreen', () => {
  it('fills the viewport: full-screen class and no width cap', () => {
    const { container } = render(BaseModal, {
      props: { isOpen: true, title: 'Reader', size: 'lg', fullscreen: true },
    });
    const el = container.querySelector('.modal-container') as HTMLElement;
    expect(el.classList.contains('fullscreen')).toBe(true);
    expect(el.style.maxWidth).toBe('');
  });

  it('keeps the size cap when not full screen (control)', () => {
    const { container } = render(BaseModal, {
      props: { isOpen: true, title: 'Dialog', size: 'lg' },
    });
    const el = container.querySelector('.modal-container') as HTMLElement;
    expect(el.classList.contains('fullscreen')).toBe(false);
    expect(el.style.maxWidth).toBe('720px');
  });
});
