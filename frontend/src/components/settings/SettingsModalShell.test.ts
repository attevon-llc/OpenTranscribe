import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import SettingsModalShell from './SettingsModalShell.svelte';
import { settingsModalStore } from '$stores/settingsModalStore';

beforeEach(() => {
  settingsModalStore.reset();
  document.body.style.overflow = '';
});

describe('SettingsModalShell', () => {
  it('is the real dialog frame, announced as busy, with sidebar and content placeholders', () => {
    const { container } = render(SettingsModalShell);

    const dialog = container.querySelector('.settings-modal');
    expect(dialog).toHaveAttribute('role', 'dialog');
    expect(dialog).toHaveAttribute('aria-busy', 'true');
    expect(dialog).toHaveAttribute('data-loading', 'true');
    expect(screen.getByRole('heading', { name: 'settings.title' })).toBeInTheDocument();
    expect(container.querySelector('.settings-sidebar .row-skeleton')).not.toBeNull();
    expect(container.querySelector('.settings-content .title-skeleton')).not.toBeNull();
    // Placeholders, not the real thing: nothing here may be mistaken for navigation.
    expect(container.querySelector('.nav-item')).toBeNull();
  });

  it('locks background scroll while shown and releases it when swapped out', () => {
    const { unmount } = render(SettingsModalShell);
    expect(document.body.style.overflow).toBe('hidden');
    unmount();
    expect(document.body.style.overflow).toBe('');
  });

  it('closes on the header button, on Escape and on a backdrop click', async () => {
    const { container } = render(SettingsModalShell);

    for (const close of [
      () => fireEvent.click(screen.getByRole('button', { name: 'settings.modal.closeSettings' })),
      () => fireEvent.keyDown(document, { key: 'Escape' }),
      () => fireEvent.click(container.querySelector('.settings-modal-backdrop') as HTMLElement),
    ]) {
      settingsModalStore.open('download');
      expect(get(settingsModalStore).isOpen).toBe(true);
      await close();
      expect(get(settingsModalStore).isOpen).toBe(false);
    }
  });
});
