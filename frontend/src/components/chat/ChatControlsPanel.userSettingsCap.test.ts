/**
 * The model picker and the reasoning-verdict lookup both read the per-user
 * `/llm-settings/configurations` list. With the `llm.user_settings` capability
 * disabled that router is not mounted (issue #1046): the picker would offer a
 * lone "Default" with nothing to switch to, and every panel open would fire a
 * request that can only 404. Each disabled-case assertion is paired with the
 * enabled case, so a panel that never rendered the picker would not pass.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

import ChatControlsPanel from './ChatControlsPanel.svelte';
import { LLMSettingsApi } from '$lib/api/llmSettings';
import { capabilities } from '$stores/capabilities';

function setUserLlmSettings(enabled: boolean) {
  capabilities.update((s) => ({
    ...s,
    capabilities: { ...s.capabilities, 'llm.user_settings': enabled },
  }));
}

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

afterEach(() => setUserLlmSettings(true));

function openPanel() {
  return render(ChatControlsPanel, { props: { isOpen: true, settings: {}, llmConfigUuid: null } });
}

describe('ChatControlsPanel and the llm.user_settings capability', () => {
  it('shows the model picker and loads configurations when users may configure LLMs', async () => {
    setUserLlmSettings(true);
    const { findByTestId } = openPanel();

    expect(await findByTestId('chat-model-select')).toBeTruthy();
    await waitFor(() => expect(LLMSettingsApi.getUserConfigurations).toHaveBeenCalled());
  });

  it('hides the picker and never asks /llm-settings when the capability is disabled', async () => {
    setUserLlmSettings(false);
    const { findByTestId, queryByTestId } = openPanel();

    expect(await findByTestId('chat-advanced-toggle')).toBeTruthy();
    expect(queryByTestId('chat-model-select')).toBeNull();
    expect(LLMSettingsApi.getUserConfigurations).not.toHaveBeenCalled();
  });
});
