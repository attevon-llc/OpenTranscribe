import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import Host from './ConfirmationModalTestHost.svelte';

describe('ConfirmationModal Enter key', () => {
  it('does not confirm while it is closed', async () => {
    const onConfirm = vi.fn();
    render(Host, { isOpen: false, onConfirm });

    await fireEvent.keyDown(document.body, { key: 'Enter' });

    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('confirms on Enter while it is open', async () => {
    const onConfirm = vi.fn();
    render(Host, { isOpen: true, onConfirm });

    await fireEvent.keyDown(document.body, { key: 'Enter' });

    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
