/** #788: while a Retry-After wait runs, sending is held and the draft is kept. */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => {
      run((key, o) => (o ? `${key}|${JSON.stringify(o)}` : key));
      return () => {};
    },
  },
}));

import ChatComposer from './ChatComposer.svelte';

describe('ChatComposer rate-limit hold', () => {
  it('disables send, names the wait, and does not dispatch or clear the draft', async () => {
    const onSend = vi.fn();
    render(ChatComposer, {
      props: { value: 'my question', blockedFor: 12 },
      events: { send: onSend },
    } as never);

    const button = screen.getByTestId('chat-send') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toBe('chat.errors.rate_limitedWait|{"seconds":12}');

    await fireEvent.keyDown(screen.getByTestId('chat-composer-input'), { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
    expect((screen.getByTestId('chat-composer-input') as HTMLTextAreaElement).value).toBe(
      'my question'
    );
  });

  it('enables send again at 0', () => {
    render(ChatComposer, { props: { value: 'my question', blockedFor: 0 } });
    expect((screen.getByTestId('chat-send') as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('control: the send event hook works when not blocked', () => {
  it('dispatches send on Enter', async () => {
    const onSend = vi.fn();
    render(ChatComposer, {
      props: { value: 'q', blockedFor: 0 },
      events: { send: onSend },
    } as never);
    await fireEvent.keyDown(screen.getByTestId('chat-composer-input'), { key: 'Enter' });
    expect(onSend).toHaveBeenCalledTimes(1);
  });
});
