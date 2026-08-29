/**
 * Bedrock is the first LLM provider with neither a `base_url` nor an `api_key` —
 * it's reached through the AWS SDK, not an HTTP endpoint (issue #596). The form
 * must not render either field for it, must explain where credentials/region
 * actually come from (a deployment-level setting, not this form), and must
 * still submit a valid configuration from just a name + model ID.
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

const BEDROCK_PROVIDER: ProviderDefaults = {
  provider: 'bedrock',
  default_model: 'anthropic.claude-haiku-4-5-20251001-v1:0',
  default_base_url: undefined,
  requires_api_key: false,
  supports_custom_url: false,
  max_context_length: undefined,
  description: 'AWS Bedrock',
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

function renderModal() {
  return render(LLMConfigModal, {
    props: {
      show: true,
      editingConfig: null,
      supportedProviders: [OPENAI_PROVIDER, BEDROCK_PROVIDER],
    },
  } as never);
}

async function selectBedrock() {
  const select = screen.getByLabelText('llm.provider') as HTMLSelectElement;
  await fireEvent.change(select, { target: { value: 'bedrock' } });
}

describe('LLMConfigModal — Bedrock provider', () => {
  it('renders no base URL field for Bedrock (SDK call, not an HTTP endpoint)', async () => {
    renderModal();
    await selectBedrock();

    expect(screen.queryByLabelText(/llm\.baseUrl/)).not.toBeInTheDocument();
  });

  it('renders no API key field for a new Bedrock config', async () => {
    renderModal();
    await selectBedrock();

    expect(screen.queryByLabelText(/llm\.apiKey/)).not.toBeInTheDocument();
  });

  it('explains that region/credentials are a deployment-level setting, not entered here', async () => {
    renderModal();
    await selectBedrock();

    expect(screen.getByText('llm.bedrockInfo')).toBeInTheDocument();
  });

  it('does not show the info note for a provider that DOES take a base URL', async () => {
    renderModal();
    const select = screen.getByLabelText('llm.provider') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: 'openai' } });

    expect(screen.queryByText('llm.bedrockInfo')).not.toBeInTheDocument();
    expect(screen.getByLabelText(/llm\.baseUrl/)).toBeInTheDocument();
  });

  it('submits a valid configuration from just a name and model ID', async () => {
    api.createSettings.mockResolvedValue({ uuid: 'new-config', provider: 'bedrock' });
    renderModal();
    await selectBedrock();

    await fireEvent.input(screen.getByLabelText('llm.configName'), {
      target: { value: 'My Bedrock Config' },
    });
    await fireEvent.input(screen.getByLabelText('llm.modelName'), {
      target: { value: 'anthropic.claude-haiku-4-5-20251001-v1:0' },
    });

    const saveButton = screen.getByRole('button', { name: /llm\.saveConfiguration/ });
    expect(saveButton).not.toBeDisabled();
    await fireEvent.click(saveButton);

    expect(api.createSettings).toHaveBeenCalledTimes(1);
    const payload = api.createSettings.mock.calls[0][0];
    expect(payload.provider).toBe('bedrock');
    expect(payload.model_name).toBe('anthropic.claude-haiku-4-5-20251001-v1:0');
  });

  it('allows testing the connection with no API key entered', async () => {
    api.testConnection.mockResolvedValue({
      success: true,
      status: 'success',
      message: 'Connection successful',
    });
    renderModal();
    await selectBedrock();

    await fireEvent.input(screen.getByLabelText('llm.modelName'), {
      target: { value: 'anthropic.claude-haiku-4-5-20251001-v1:0' },
    });

    const testButton = screen.getByRole('button', { name: /llm\.testConnection/ });
    expect(testButton).not.toBeDisabled();
    await fireEvent.click(testButton);

    expect(api.testConnection).toHaveBeenCalledTimes(1);
    const request = api.testConnection.mock.calls[0][0];
    expect(request.provider).toBe('bedrock');
    expect(request.api_key).toBeUndefined();
  });
});
