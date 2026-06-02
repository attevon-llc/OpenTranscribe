import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

// Mock the clipboard util so we can assert the call and trigger onSuccess.
vi.mock('$lib/utils/clipboard', () => ({
  copyToClipboard: vi.fn(async (_text: string, onSuccess?: () => void) => {
    onSuccess?.();
    return { success: true };
  }),
}));

import { copyToClipboard } from '$lib/utils/clipboard';
import CopyButton from './CopyButton.svelte';

describe('CopyButton', () => {
  beforeEach(() => {
    vi.mocked(copyToClipboard).mockClear();
  });

  it('renders the idle label', () => {
    render(CopyButton, { props: { text: 'hello', label: 'Copy text' } });
    expect(screen.getByRole('button', { name: /Copy text/ })).toBeInTheDocument();
  });

  it('copies, dispatches `copied`, and swaps to the copied label', async () => {
    const onCopied = vi.fn();
    render(CopyButton, {
      props: { text: 'hello', label: 'Copy text', copiedLabel: 'Copied!' },
      events: { copied: onCopied },
    });

    await fireEvent.click(screen.getByRole('button'));

    expect(copyToClipboard).toHaveBeenCalledWith(
      'hello',
      expect.any(Function),
      expect.any(Function)
    );
    expect(onCopied).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Copied!')).toBeInTheDocument();
  });

  it('uses the label as aria-label when iconOnly', () => {
    render(CopyButton, { props: { text: 'x', label: 'Copy link', iconOnly: true } });
    expect(screen.getByRole('button', { name: 'Copy link' })).toBeInTheDocument();
  });
});
