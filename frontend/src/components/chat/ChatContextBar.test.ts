/**
 * W2.2 fixes to ChatContextBar: a speakers-only scope (no files/collections/
 * tags) previously had NO way back to "all transcripts" — `hasAnyScope`
 * (recordings OR speakers) now gates clear-all instead of the recordings axis
 * alone. Speaker chips also moved from one combined summary chip to
 * individually dismissible chips.
 */
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, opts?: Record<string, unknown>) => string) => void) => (
      run((k, opts) => (opts ? `${k}:${JSON.stringify(opts)}` : k)), () => {}
    ),
  },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));

import ChatContextBar from './ChatContextBar.svelte';
import ChatContextBarTestHost from './ChatContextBarTestHost.svelte';
import { emptyScope, type ChatScope } from '$lib/types/chat';

function scope(overrides: Partial<ChatScope> = {}): ChatScope {
  return { ...emptyScope(), ...overrides };
}

describe('ChatContextBar — clear-all visibility', () => {
  it('no clear-all for a genuinely unfiltered scope', () => {
    const { queryByTestId } = render(ChatContextBar, { props: { scope: emptyScope() } });
    expect(queryByTestId('chat-scope-clear')).toBeNull();
  });

  it('clear-all appears for a recordings-only scope (existing behaviour)', () => {
    const { getByTestId } = render(ChatContextBar, {
      props: { scope: scope({ file_uuids: ['f1'] }) },
    });
    expect(getByTestId('chat-scope-clear').textContent).toContain('chat.context.clearAll');
  });

  it('clear-all appears for a SPEAKERS-ONLY scope', () => {
    // The bug: isScopeEmpty only looks at files/collections/tags, so a
    // speakers-only scope used to render the "All transcripts" chip with no
    // way back — the filter was live but nothing offered to clear it.
    const { getByTestId } = render(ChatContextBar, {
      props: { scope: scope({ speakers: ['Dana'] }) },
    });
    expect(getByTestId('chat-scope-clear').textContent).toContain('chat.context.clearAll');
  });

  it('the "All transcripts" chip still renders alongside a speaker filter', () => {
    const { getByTestId } = render(ChatContextBar, {
      props: { scope: scope({ speakers: ['Dana'] }) },
    });
    expect(getByTestId('chat-scope-all').textContent).toContain('chat.context.allTranscripts');
  });

  it('no clear-all when context is off, regardless of scope', () => {
    const { queryByTestId } = render(ChatContextBar, {
      props: { scope: scope({ speakers: ['Dana'] }), useContext: false },
    });
    expect(queryByTestId('chat-scope-clear')).toBeNull();
  });
});

describe('ChatContextBar — individually dismissible speaker chips', () => {
  it('renders one chip per speaker', () => {
    const { getAllByTestId } = render(ChatContextBar, {
      props: { scope: scope({ speakers: ['Dana', 'Alex'] }) },
    });
    const chips = getAllByTestId('chat-scope-speaker');
    expect(chips).toHaveLength(2);
    expect(chips.map((c) => c.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining('Dana'), expect.stringContaining('Alex')])
    );
  });

  it("clicking a chip's remove button dispatches removeSpeaker with that name only", async () => {
    let removed: string | undefined;
    const { getAllByTestId } = render(ChatContextBarTestHost, {
      props: {
        scope: scope({ speakers: ['Dana', 'Alex'] }),
        onRemoveSpeaker: (name: string) => {
          removed = name;
        },
      },
    });
    const removeButtons = getAllByTestId('chat-scope-speaker-remove');
    expect(removeButtons).toHaveLength(2);
    removeButtons[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(removed).toBe('Dana');
  });

  it('no speaker chips at all when the scope carries none', () => {
    const { queryByTestId } = render(ChatContextBar, { props: { scope: emptyScope() } });
    expect(queryByTestId('chat-scope-speaker')).toBeNull();
  });
});
