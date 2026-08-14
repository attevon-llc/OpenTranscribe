/**
 * The reasoning toggle renders ONLY where the off-switch was measured (#64).
 *
 * A provider returning HTTP 200 for a "do not reason" parameter is not evidence
 * the model honoured it: measured against a real vLLM serving gemma-4-e4b,
 * `enable_thinking: false` produced 931 characters of reasoning — byte-identical
 * to omitting the parameter entirely. A toggle over that model would tell the
 * user reasoning is off while the model reasons anyway, which is worse than no
 * toggle at all.
 *
 * So the server reports a per-model verdict and only `'works'` may render the
 * control. Every "it renders" case below is paired with a case that must render
 * nothing — a component that always showed the toggle would otherwise pass.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';

import ChatControlsPanel from './ChatControlsPanel.svelte';
import type { ReasoningOffSwitch, UserLLMConfigurationsList } from '$lib/api/llmSettings';
import { LLMSettingsApi } from '$lib/api/llmSettings';

const PINNED_UUID = 'config-pinned';
const ACTIVE_UUID = 'config-active';

function configurations(verdicts: Record<string, ReasoningOffSwitch>): UserLLMConfigurationsList {
  return {
    configurations: [],
    shared_configurations: [],
    active_configuration_id: ACTIVE_UUID,
    total: 0,
    reasoning_off_switch: verdicts,
  };
}

function openPanel(llmConfigUuid: string | null = PINNED_UUID) {
  return render(ChatControlsPanel, {
    props: { isOpen: true, settings: {}, llmConfigUuid },
  });
}

describe('ChatControlsPanel — reasoning toggle (#64)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the toggle when the probe measured a working off-switch', async () => {
    vi.spyOn(LLMSettingsApi, 'getUserConfigurations').mockResolvedValue(
      configurations({ [PINNED_UUID]: 'works' })
    );

    const { findByTestId } = openPanel();

    expect(await findByTestId('chat-reasoning-toggle')).toBeTruthy();
  });

  it('renders nothing when the probe proved the model ignores the parameter', async () => {
    // The measured gemma-4-e4b case: the provider accepted `false` and reasoned anyway.
    vi.spyOn(LLMSettingsApi, 'getUserConfigurations').mockResolvedValue(
      configurations({ [PINNED_UUID]: 'absent' })
    );

    const { queryByTestId, findByTestId } = openPanel();

    // Wait for a control that IS unconditional, so this is not just asserting
    // against a panel that has not finished loading.
    expect(await findByTestId('chat-advanced-toggle')).toBeTruthy();
    await waitFor(() => expect(LLMSettingsApi.getUserConfigurations).toHaveBeenCalled());
    expect(queryByTestId('chat-reasoning-toggle')).toBeNull();
  });

  it('renders nothing for a model that has never been probed', async () => {
    vi.spyOn(LLMSettingsApi, 'getUserConfigurations').mockResolvedValue(configurations({}));

    const { queryByTestId, findByTestId } = openPanel();

    expect(await findByTestId('chat-advanced-toggle')).toBeTruthy();
    await waitFor(() => expect(LLMSettingsApi.getUserConfigurations).toHaveBeenCalled());
    expect(queryByTestId('chat-reasoning-toggle')).toBeNull();
  });

  it('falls back to the account-default configuration when none is pinned', async () => {
    vi.spyOn(LLMSettingsApi, 'getUserConfigurations').mockResolvedValue(
      configurations({ [ACTIVE_UUID]: 'works' })
    );

    const { findByTestId } = openPanel(null);

    expect(await findByTestId('chat-reasoning-toggle')).toBeTruthy();
  });

  it('renders nothing when the capability lookup fails', async () => {
    vi.spyOn(LLMSettingsApi, 'getUserConfigurations').mockRejectedValue(new Error('offline'));

    const { queryByTestId, findByTestId } = openPanel();

    expect(await findByTestId('chat-advanced-toggle')).toBeTruthy();
    await waitFor(() => expect(LLMSettingsApi.getUserConfigurations).toHaveBeenCalled());
    expect(queryByTestId('chat-reasoning-toggle')).toBeNull();
  });

  it('turning it off dispatches reasoning: false, and back on dispatches null', async () => {
    vi.spyOn(LLMSettingsApi, 'getUserConfigurations').mockResolvedValue(
      configurations({ [PINNED_UUID]: 'works' })
    );

    const changes: Array<Record<string, unknown>> = [];
    const { findByTestId } = render(ChatControlsPanel, {
      props: { isOpen: true, settings: {}, llmConfigUuid: PINNED_UUID },
      events: {
        change: (e: CustomEvent) => changes.push(e.detail as Record<string, unknown>),
      },
    } as never);

    const toggle = (await findByTestId('chat-reasoning-toggle')) as HTMLInputElement;
    // Checked by default: `reasoning` is unset, which inherits the model's behaviour.
    expect(toggle.checked).toBe(true);

    await fireEvent.click(toggle);
    expect(changes).toEqual([{ reasoning: false }]);

    await fireEvent.click(toggle);
    expect(changes).toEqual([{ reasoning: false }, { reasoning: null }]);
  });

  it('reflects a stored off preference as an unchecked box', async () => {
    vi.spyOn(LLMSettingsApi, 'getUserConfigurations').mockResolvedValue(
      configurations({ [PINNED_UUID]: 'works' })
    );

    const { findByTestId } = render(ChatControlsPanel, {
      props: { isOpen: true, settings: { reasoning: false }, llmConfigUuid: PINNED_UUID },
    });

    const toggle = (await findByTestId('chat-reasoning-toggle')) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
  });
});
