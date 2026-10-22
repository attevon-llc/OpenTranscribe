/**
 * "Answer length" must show the real default and the real ceiling for the model in play.
 *
 * The field used to say only "Default", with a hard-coded 200,000 maximum. The turn,
 * meanwhile, sent the model's derived reply budget and silently clamped any larger override
 * to half the context window (or a tenant cap) — the same shape as the 8192 `max_tokens`
 * that silently capped four evaluation runs. Both numbers now come from
 * `GET /chat/answer-budget`, computed by the function the stream itself uses.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';

import ChatControlsPanel from './ChatControlsPanel.svelte';
import * as chatApi from '$lib/api/chatApi';
import { LLMSettingsApi } from '$lib/api/llmSettings';

const BUDGET = { default_tokens: 4000, max_tokens: 16384, context_window: 32768 };

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(LLMSettingsApi, 'getUserConfigurations').mockResolvedValue({
    configurations: [],
    shared_configurations: [],
    active_configuration_id: null,
    total: 0,
    reasoning_off_switch: {},
  } as never);
});

function openPanel(
  settings = {},
  llmConfigUuid: string | null = null,
  onChange: (detail: unknown) => void = () => {}
) {
  return render(ChatControlsPanel, {
    props: { isOpen: true, settings, llmConfigUuid },
    events: { change: (e: CustomEvent) => onChange(e.detail) },
  });
}

describe('ChatControlsPanel answer length', () => {
  it('shows the model default as the placeholder and value, and bounds the field by the real ceiling', async () => {
    vi.spyOn(chatApi, 'getAnswerBudget').mockResolvedValue(BUDGET);
    const { findByTestId, getByTestId } = openPanel();

    const value = await findByTestId('chat-max-tokens-value');
    await waitFor(() => expect(value.textContent).toContain((4000).toLocaleString()));
    const input = getByTestId('chat-max-tokens') as HTMLInputElement;
    expect(input.placeholder).toBe('4000');
    expect(input.max).toBe('16384');
    expect(getByTestId('chat-max-tokens-default')).toBeTruthy();
  });

  it('asks for the pinned model, and asks again when the pin changes', async () => {
    const spy = vi
      .spyOn(chatApi, 'getAnswerBudget')
      .mockImplementation(async (uuid) =>
        uuid === 'cfg-b'
          ? { default_tokens: 16384, max_tokens: 65536, context_window: 131072 }
          : BUDGET
      );
    const { rerender, getByTestId } = openPanel({}, 'cfg-a');
    const input = () => getByTestId('chat-max-tokens') as HTMLInputElement;
    await waitFor(() => expect(input().placeholder).toBe('4000'));
    expect(spy).toHaveBeenCalledWith('cfg-a');

    await rerender({ isOpen: true, settings: {}, llmConfigUuid: 'cfg-b' });
    await waitFor(() => expect(input().placeholder).toBe('16384'));
    expect(input().max).toBe('65536');
    expect(spy).toHaveBeenCalledWith('cfg-b');
  });

  it('says so when an override exceeds what this model will honour', async () => {
    vi.spyOn(chatApi, 'getAnswerBudget').mockResolvedValue(BUDGET);
    const { findByTestId, getByTestId } = openPanel({ max_tokens: 50000 });

    expect(await findByTestId('chat-max-tokens-clamped')).toBeTruthy();
    expect(getByTestId('chat-max-tokens-value').textContent).toContain((50000).toLocaleString());
  });

  it('shows no clamp notice for an override within the ceiling', async () => {
    vi.spyOn(chatApi, 'getAnswerBudget').mockResolvedValue(BUDGET);
    const { findByTestId, queryByTestId } = openPanel({ max_tokens: 8000 });

    await findByTestId('chat-max-tokens-default');
    expect(queryByTestId('chat-max-tokens-clamped')).toBeNull();
  });

  it('offers a reset only when an override is set, and reset returns to the default', async () => {
    vi.spyOn(chatApi, 'getAnswerBudget').mockResolvedValue(BUDGET);
    const unset = openPanel();
    await unset.findByTestId('chat-max-tokens-default');
    expect(unset.queryByTestId('chat-max-tokens-reset')).toBeNull();
    unset.unmount();

    const onChange = vi.fn();
    const set = openPanel({ max_tokens: 8000 }, null, onChange);
    await fireEvent.click(await set.findByTestId('chat-max-tokens-reset'));
    expect(onChange).toHaveBeenCalledWith({ max_tokens: null });
  });

  it('degrades to the generic label when the budget cannot be read', async () => {
    const spy = vi.spyOn(chatApi, 'getAnswerBudget').mockRejectedValue(new Error('no llm'));
    const { getByTestId, queryByTestId } = openPanel({ max_tokens: 50000 });
    await waitFor(() => expect(spy).toHaveBeenCalled());

    expect(queryByTestId('chat-max-tokens-default')).toBeNull();
    expect(queryByTestId('chat-max-tokens-clamped')).toBeNull();
    expect((getByTestId('chat-max-tokens') as HTMLInputElement).max).toBe('200000');
  });
});
