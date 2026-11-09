import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

const clipboard = vi.hoisted(() => ({
  copyToClipboard: vi.fn((_text: string, ok?: () => void) => ok?.()),
}));
vi.mock('$lib/utils/clipboard', () => clipboard);

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import PromptCopyButton from './PromptCopyButton.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

const button = (c: HTMLElement) => c.querySelector('button') as HTMLButtonElement;

describe('PromptCopyButton', () => {
  it('copies the text and shows the copied state for two seconds', async () => {
    const { container } = render(PromptCopyButton, { props: { text: 'hello' } });
    expect(button(container).title).toBe('prompts.copy');
    await fireEvent.click(button(container));
    expect(clipboard.copyToClipboard).toHaveBeenCalledWith(
      'hello',
      expect.any(Function),
      expect.any(Function)
    );
    expect(button(container).classList.contains('copied')).toBe(true);
    expect(button(container).title).toBe('prompts.copiedToClipboard');
    await vi.advanceTimersByTimeAsync(1999);
    expect(button(container).classList.contains('copied')).toBe(true);
    await vi.advanceTimersByTimeAsync(1);
    expect(button(container).classList.contains('copied')).toBe(false);
  });

  it('does nothing for empty text', async () => {
    const { container } = render(PromptCopyButton, { props: { text: '' } });
    await fireEvent.click(button(container));
    expect(clipboard.copyToClipboard).not.toHaveBeenCalled();
  });

  it('stays in its default state when the copy fails', async () => {
    clipboard.copyToClipboard.mockImplementationOnce(
      (_t: string, _ok?: () => void, fail?: (e: string) => void) => fail?.('denied')
    );
    const { container } = render(PromptCopyButton, { props: { text: 'x' } });
    await fireEvent.click(button(container));
    expect(button(container).classList.contains('copied')).toBe(false);
  });
});
