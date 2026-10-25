/**
 * The message list's scrollbar belongs at the far edge of the chat pane, not hugging the
 * centred 52rem column. The scroller is `.thread` in ChatThread, so no ancestor between it and
 * `.chat-main` may cap its width; the column is re-centred with inline padding instead.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { resolve } from 'path';

const page = readFileSync(
  resolve(__dirname, '../../routes/chat/[[conversationId]]/+page.svelte'),
  'utf8'
);
const thread = readFileSync(resolve(__dirname, './ChatThread.svelte'), 'utf8');

function rule(src: string, selector: string): string {
  const m = src.match(new RegExp(`(?:^|\\n)\\s*${selector.replace('.', '\\.')}\\s*\\{([^}]*)\\}`));
  if (!m) throw new Error(`rule ${selector} not found`);
  return m[1];
}

describe('chat scrollbar edge', () => {
  it('does not width-cap or auto-centre .chat-body (the scroller ancestor)', () => {
    const body = rule(page, '.chat-body');
    expect(body).not.toMatch(/max-width/);
    expect(body).not.toMatch(/margin:\s*0 auto/);
  });

  it('centres the message column inside the full-width scroller with logical padding', () => {
    const t = rule(thread, '.thread');
    expect(t).toMatch(/overflow-y:\s*auto/);
    expect(t).toMatch(/padding-inline:[^;]*52rem/);
  });

  it('keeps the composer footer on the same 52rem measure', () => {
    expect(rule(page, '.chat-footer')).toMatch(/max-width:\s*52rem/);
  });
});
