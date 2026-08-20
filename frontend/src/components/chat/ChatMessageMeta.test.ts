/**
 * ChatMessageMeta renders an ALLOWLIST of msg_metadata keys — an unlisted key
 * is invisible, so a later lane's metadata would silently never render no
 * matter how carefully the backend populates it. This pins that the Wave 2
 * keys (`map_source`, `speaker_resolution`, `plan`, `legs_failed`,
 * `llm_calls`, `router_language_unmatched`) are on the allowlist, each with a
 * silent-when-absent control so the panel does not grow a permanent row.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';

import ChatMessageMeta from './ChatMessageMeta.svelte';
import ChatMessageMetaDisambiguateTestHost from './ChatMessageMetaDisambiguateTestHost.svelte';
import type { ChatMessage as ChatMessageType } from '$lib/types/chat';

function assistantMessage(overrides: Partial<ChatMessageType> = {}): ChatMessageType {
  return {
    uuid: 'assistant-1',
    role: 'assistant',
    content: 'The team shipped on Tuesday.',
    status: 'complete',
    ...overrides,
  };
}

async function expandPanel(getByTestId: (id: string) => HTMLElement) {
  const toggle = getByTestId('chat-meta-toggle');
  toggle.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 0));
}

describe('ChatMessageMeta — Wave 2 metadata keys', () => {
  it('renders map_source when present', async () => {
    const { getByTestId, container } = render(ChatMessageMeta, {
      props: { message: assistantMessage({ msg_metadata: { map_source: 'llm-batch' } }) },
    });
    await expandPanel(getByTestId);
    expect(container.textContent).toContain('llm-batch');
  });

  it('renders llm_calls, including zero (a real reported value, not "absent")', async () => {
    const { getByTestId, container } = render(ChatMessageMeta, {
      props: { message: assistantMessage({ msg_metadata: { llm_calls: 0 } }) },
    });
    await expandPanel(getByTestId);
    expect(container.textContent).toContain('0');
  });

  it('renders legs_failed as a joined list', async () => {
    const { getByTestId, container } = render(ChatMessageMeta, {
      props: {
        message: assistantMessage({
          msg_metadata: { legs_failed: ['aggregation', 'digest_map'] },
        }),
      },
    });
    await expandPanel(getByTestId);
    expect(container.textContent).toContain('aggregation');
    expect(container.textContent).toContain('digest_map');
  });

  it('renders speaker_resolution matched names', async () => {
    const { getByTestId, container } = render(ChatMessageMeta, {
      props: {
        message: assistantMessage({
          msg_metadata: {
            speaker_resolution: { matched: ['Dana Whitfield'], ambiguous: [] },
          },
        }),
      },
    });
    await expandPanel(getByTestId);
    expect(container.textContent).toContain('Dana Whitfield');
  });

  it('renders speaker_resolution ambiguous names', async () => {
    const { getByTestId, container } = render(ChatMessageMeta, {
      props: {
        message: assistantMessage({
          msg_metadata: { speaker_resolution: { matched: [], ambiguous: ['Alex'] } },
        }),
      },
    });
    await expandPanel(getByTestId);
    expect(container.textContent).toContain('Alex');
  });

  it('W2.2: renders one disambiguation chip per ambiguous candidate', async () => {
    const { getByTestId, getAllByTestId } = render(ChatMessageMeta, {
      props: {
        message: assistantMessage({
          msg_metadata: {
            speaker_resolution: { matched: [], ambiguous: ['Alice', 'Alex'] },
          },
        }),
      },
    });
    await expandPanel(getByTestId);
    const chips = getAllByTestId('chat-speaker-disambiguation-chip');
    expect(chips.map((c) => c.textContent?.trim())).toEqual(['Alice', 'Alex']);
  });

  it('W2.2: clicking a disambiguation chip dispatches the candidate name', async () => {
    let picked: string | undefined;
    const { getByTestId, getAllByTestId } = render(ChatMessageMetaDisambiguateTestHost, {
      props: {
        message: assistantMessage({
          msg_metadata: { speaker_resolution: { matched: [], ambiguous: ['Alice'] } },
        }),
        onDisambiguate: (name: string) => {
          picked = name;
        },
      },
    });
    await expandPanel(getByTestId);

    getAllByTestId('chat-speaker-disambiguation-chip')[0].dispatchEvent(
      new MouseEvent('click', { bubbles: true })
    );
    expect(picked).toBe('Alice');
  });

  it('W2.2: no disambiguation row when there are no ambiguous candidates', async () => {
    const { getByTestId, queryByTestId } = render(ChatMessageMeta, {
      props: {
        message: assistantMessage({
          msg_metadata: { speaker_resolution: { matched: ['Dana'], ambiguous: [] } },
        }),
      },
    });
    await expandPanel(getByTestId);
    expect(queryByTestId('chat-speaker-disambiguation')).toBeNull();
  });

  it('renders plan steps joined with an arrow', async () => {
    const { getByTestId, container } = render(ChatMessageMeta, {
      props: {
        message: assistantMessage({
          msg_metadata: { plan: { steps: ['resolve scope', 'aggregate'] } },
        }),
      },
    });
    await expandPanel(getByTestId);
    expect(container.textContent).toContain('resolve scope');
    expect(container.textContent).toContain('aggregate');
  });

  it('renders router_language_unmatched as an affirmative row', async () => {
    const { getByTestId, container } = render(ChatMessageMeta, {
      props: { message: assistantMessage({ msg_metadata: { router_language_unmatched: true } }) },
    });
    await expandPanel(getByTestId);
    expect(container.textContent).toContain('chat.meta.routerLanguageUnmatched');
  });

  it('the toggle appears when ONLY a Wave 2 key is present', () => {
    // Before this fix, `hasContent` did not check any of these keys, so a
    // message carrying nothing else silently rendered no panel at all.
    const { getByTestId } = render(ChatMessageMeta, {
      props: { message: assistantMessage({ msg_metadata: { map_source: 'code' } }) },
    });
    expect(getByTestId('chat-meta-toggle')).toBeTruthy();
  });

  it('stays silent (no panel) when none of the Wave 2 keys are present', () => {
    // The control: without it, a component that always renders would pass
    // every test above trivially.
    const { queryByTestId } = render(ChatMessageMeta, {
      props: { message: assistantMessage({ msg_metadata: {} }) },
    });
    expect(queryByTestId('chat-meta-toggle')).toBeNull();
  });

  it('does not render a Wave 2 row when its key is absent, even with the panel open', async () => {
    const { getByTestId, queryByTestId } = render(ChatMessageMeta, {
      props: { message: assistantMessage({ msg_metadata: { llm_calls: 2 } }) },
    });
    await expandPanel(getByTestId);
    // llm_calls forces the panel open, but map_source was never set.
    const grid = getByTestId('chat-meta-grid');
    expect(grid.textContent).not.toContain('llm-batch');
    expect(queryByTestId('chat-meta-toggle')).toBeTruthy();
  });
});

describe('ChatMessageMeta — scope_files_dropped (Finding #4)', () => {
  it('renders the drop count when present', async () => {
    const { getByTestId } = render(ChatMessageMeta, {
      props: { message: assistantMessage({ msg_metadata: { scope_files_dropped: 37 } }) },
    });
    await expandPanel(getByTestId);
    expect(getByTestId('chat-meta-scope-files-dropped').textContent).toContain('37');
  });

  it('the toggle appears when ONLY scope_files_dropped is present', () => {
    const { getByTestId } = render(ChatMessageMeta, {
      props: { message: assistantMessage({ msg_metadata: { scope_files_dropped: 5 } }) },
    });
    expect(getByTestId('chat-meta-toggle')).toBeTruthy();
  });

  it('does not render the row when scope_files_dropped is absent (an ordinary turn)', async () => {
    const { getByTestId, queryByTestId } = render(ChatMessageMeta, {
      props: { message: assistantMessage({ msg_metadata: { llm_calls: 1 } }) },
    });
    await expandPanel(getByTestId);
    expect(queryByTestId('chat-meta-scope-files-dropped')).toBeNull();
  });

  it('does not render the row when scope_files_dropped is zero', async () => {
    // The backend only ever sets this key when something was actually
    // dropped (never a bare 0) — but the component itself must not render an
    // empty/misleading "0 files dropped" row if it somehow arrived anyway.
    const { getByTestId, queryByTestId } = render(ChatMessageMeta, {
      props: {
        message: assistantMessage({ msg_metadata: { scope_files_dropped: 0, llm_calls: 1 } }),
      },
    });
    await expandPanel(getByTestId);
    expect(queryByTestId('chat-meta-scope-files-dropped')).toBeNull();
  });
});
