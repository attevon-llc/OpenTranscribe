/**
 * Issue #873: LLMConfigModal hardcoded `max_tokens: 4096` on every new
 * configuration regardless of what the selected model's provider actually
 * supports — a downgrade even from the backend schema's own 8192 default,
 * and a severe one for a 128k/200k-context provider. The server already
 * returns `max_context_length` per provider in the catalog
 * (`GET /llm-settings/providers`); the modal declared the field
 * (`ProviderDefaults.max_context_length`) but never read it.
 *
 * This file watches that failure directly: selecting a high-context provider
 * must populate `max_tokens` from the catalog, not leave it at a literal
 * 4096. An assertion that merely checks "a value is present" would pass
 * against the old code (4096 is a value), so every assertion here pins the
 * actual number.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import type { ProviderDefaults } from '../../lib/api/llmSettings';

const api = vi.hoisted(() => ({
  createSettings: vi.fn(),
  updateSettings: vi.fn(),
  testConnection: vi.fn(),
  getConfigApiKey: vi.fn(),
}));

vi.mock('../../lib/api/llmSettings', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api/llmSettings')>(
    '../../lib/api/llmSettings'
  );
  return { ...actual, LLMSettingsApi: { ...actual.LLMSettingsApi, ...api } };
});

const mockToast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('../../stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import LLMConfigModal from './LLMConfigModal.svelte';

// A deliberately extreme context window so a stray "8192" or "4096" fallback
// cannot be mistaken for the catalog value being read correctly.
const HUGE_CONTEXT_PROVIDER: ProviderDefaults = {
  provider: 'openrouter',
  default_model: 'anthropic/claude-3.5-haiku',
  default_base_url: 'https://openrouter.ai/api/v1',
  requires_api_key: true,
  supports_custom_url: false,
  max_context_length: 200000,
  description: 'OpenRouter',
};

const OPENAI_PROVIDER: ProviderDefaults = {
  provider: 'openai',
  default_model: 'gpt-4o-mini',
  default_base_url: 'https://api.openai.com/v1',
  requires_api_key: true,
  supports_custom_url: true,
  max_context_length: 128000,
  description: 'OpenAI',
};

// No catalog entry at all — the fallback path (declared schema default,
// never a literal below it).
const NO_CATALOG_PROVIDER: ProviderDefaults = {
  provider: 'bedrock',
  default_model: 'anthropic.claude-haiku-4-5-20251001-v1:0',
  default_base_url: undefined,
  requires_api_key: false,
  supports_custom_url: false,
  max_context_length: undefined,
  description: 'AWS Bedrock',
};

function renderModal() {
  return render(LLMConfigModal, {
    props: {
      show: true,
      editingConfig: null,
      supportedProviders: [OPENAI_PROVIDER, HUGE_CONTEXT_PROVIDER, NO_CATALOG_PROVIDER],
    },
  } as never);
}

async function selectProvider(value: string) {
  const select = screen.getByLabelText('llm.provider') as HTMLSelectElement;
  await fireEvent.change(select, { target: { value } });
}

function getMaxTokensInput(): HTMLInputElement {
  return screen.getByLabelText('llm.maxTokens') as HTMLInputElement;
}

describe('LLMConfigModal — context window default (issue #873)', () => {
  it('populates max_tokens from the provider catalog, not a hardcoded 4096, for a high-context provider', async () => {
    renderModal();
    await selectProvider('openrouter');

    expect(getMaxTokensInput().value).toBe('200000');
  });

  it('submits the catalog context window in the create payload, not 4096', async () => {
    api.createSettings.mockResolvedValue({ uuid: 'new-config', provider: 'openai' });
    renderModal();
    await selectProvider('openai');

    await fireEvent.input(screen.getByLabelText('llm.configName'), {
      target: { value: 'My OpenAI Config' },
    });
    await fireEvent.input(screen.getByLabelText(/llm\.apiKey/), {
      target: { value: 'sk-test' },
    });

    const saveButton = screen.getByRole('button', { name: /llm\.saveConfiguration/ });
    await fireEvent.click(saveButton);

    expect(api.createSettings).toHaveBeenCalledTimes(1);
    const payload = api.createSettings.mock.calls[0][0];
    expect(payload.max_tokens).toBe(128000);
    expect(payload.max_tokens).not.toBe(4096);
  });

  it('switching providers updates max_tokens to the new provider catalog value', async () => {
    renderModal();
    await selectProvider('openai');
    expect(getMaxTokensInput().value).toBe('128000');

    await selectProvider('openrouter');
    expect(getMaxTokensInput().value).toBe('200000');
  });

  it('falls back to the schema default (8192), never the literal 4096, when the catalog has no entry', async () => {
    renderModal();
    await selectProvider('bedrock');

    expect(getMaxTokensInput().value).toBe('8192');
  });

  it('does not fight a manually-entered max_tokens value on subsequent re-renders', async () => {
    renderModal();
    await selectProvider('openai');

    const input = getMaxTokensInput();
    await fireEvent.input(input, { target: { value: '65536' } });
    expect(input.value).toBe('65536');

    // Trigger an unrelated reactive re-run (typing elsewhere touches `formData`)
    await fireEvent.input(screen.getByLabelText('llm.configName'), {
      target: { value: 'Custom name' },
    });

    expect(getMaxTokensInput().value).toBe('65536');
  });

  it('the client-side minimum agrees with the server-side validator (>= 512)', async () => {
    renderModal();
    await selectProvider('openai');

    expect(getMaxTokensInput()).toHaveAttribute('min', '512');
  });
});
