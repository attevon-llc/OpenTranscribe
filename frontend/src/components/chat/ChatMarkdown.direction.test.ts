/**
 * Mixed-direction text in chat (issue #1243 item 12).
 *
 * Model answers and user prompts are frequently English while the UI runs in `ar` (RTL).
 * Inheriting the page direction rendered an English answer right-aligned with its trailing
 * punctuation flipped to the front ("found." -> ".found"). Each message body now takes its
 * direction from its own first strong character (`dir="auto"`), as current chat products do.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';

import ChatMarkdown from './ChatMarkdown.svelte';
import ChatMessage from './ChatMessage.svelte';

describe('chat message direction', () => {
  it('assistant markdown resolves its own direction', () => {
    const { container } = render(ChatMarkdown, { props: { content: 'Hello **world**.' } });
    expect(container.querySelector('.chat-markdown')?.getAttribute('dir')).toBe('auto');
  });

  it('user text resolves its own direction', () => {
    const { container } = render(ChatMessage, {
      props: { message: { uuid: 'u1', role: 'user', content: 'مرحبا', status: 'complete' } },
    });
    expect(container.querySelector('.user-text')?.getAttribute('dir')).toBe('auto');
  });
});
