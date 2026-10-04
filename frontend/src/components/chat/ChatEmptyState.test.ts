/**
 * The no-LLM empty state deep-links to the LLM-provider settings section. That
 * section is hidden when the `llm.user_settings` capability is disabled (the
 * provider is then set by the operator via env), so the button would open a
 * modal without the thing it promised (issue #1046). There, the user can only
 * be told to ask an administrator.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));
vi.mock('$components/RetrievalQualityNotice.svelte', () => ({ default: () => {} }));

import ChatEmptyState from './ChatEmptyState.svelte';
import { capabilities } from '$stores/capabilities';

function setUserLlmSettings(enabled: boolean) {
  capabilities.update((s) => ({
    ...s,
    capabilities: { ...s.capabilities, 'llm.user_settings': enabled },
  }));
}

beforeEach(() => setUserLlmSettings(true));

describe('ChatEmptyState without an LLM', () => {
  it('offers the settings deep-link when users can configure a provider', () => {
    render(ChatEmptyState, { props: { llmAvailable: false } });

    expect(screen.getByTestId('chat-open-llm-settings')).toBeInTheDocument();
    expect(screen.getByText('chat.setup.noLlmMessage')).toBeInTheDocument();
  });

  it('points to an administrator instead when llm.user_settings is disabled', () => {
    setUserLlmSettings(false);
    render(ChatEmptyState, { props: { llmAvailable: false } });

    expect(screen.queryByTestId('chat-open-llm-settings')).toBeNull();
    expect(screen.getByText('chat.setup.noLlmMessageManaged')).toBeInTheDocument();
  });
});
