import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { ProviderDefaults } from '$lib/api/llmSettings';

const api = vi.hoisted(() => ({
  getOllamaModels: vi.fn(),
  getOpenAICompatibleModels: vi.fn(),
  getAnthropicModels: vi.fn(),
}));
vi.mock('$lib/api/llmSettings', () => ({ LLMSettingsApi: api }));
const toast = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import {
  discoverAnthropicModels,
  discoverOllamaModels,
  discoverOpenAIModels,
} from './llmModelDiscovery';

const t = (k: string) => k;
const mk = () => ({ t, setLoading: vi.fn() });

beforeEach(() => vi.clearAllMocks());

describe('discoverOllamaModels', () => {
  it('needs a base URL and does not touch the loading flag without one', async () => {
    const ctx = mk();
    expect(await discoverOllamaModels(ctx, '')).toBeNull();
    expect(toast.error).toHaveBeenCalledWith('llm.enterBaseUrlFirst');
    expect(ctx.setLoading).not.toHaveBeenCalled();
  });
  it('returns the models and always clears loading', async () => {
    const ctx = mk();
    api.getOllamaModels.mockResolvedValue({ success: true, models: [{ name: 'a' }], message: '' });
    expect(await discoverOllamaModels(ctx, 'http://o')).toEqual([{ name: 'a' }]);
    expect(ctx.setLoading.mock.calls).toEqual([[true], [false]]);
  });
  it('toasts the server message on refusal and a fallback on transport failure', async () => {
    api.getOllamaModels.mockResolvedValueOnce({ success: false, models: [], message: 'refused' });
    expect(await discoverOllamaModels(mk(), 'http://o')).toBeNull();
    expect(toast.error).toHaveBeenCalledWith('refused');
    api.getOllamaModels.mockRejectedValueOnce({});
    expect(await discoverOllamaModels(mk(), 'http://o')).toBeNull();
    expect(toast.error).toHaveBeenLastCalledWith('llm.ollamaLoadFailed');
  });
});

describe('discoverOpenAIModels', () => {
  const needsKey = { requires_api_key: true } as ProviderDefaults;
  it('requires a typed key unless the edited config has one stored', async () => {
    expect(
      await discoverOpenAIModels(mk(), { baseUrl: 'u', apiKey: '', provider: needsKey }, null)
    ).toBeNull();
    expect(toast.error).toHaveBeenCalledWith('llm.enterApiKeyFirst');
    api.getOpenAICompatibleModels.mockResolvedValue({
      success: true,
      models: [{ id: 'm' }],
      message: '',
    });
    const r = await discoverOpenAIModels(
      mk(),
      { baseUrl: 'u', apiKey: '', provider: needsKey },
      { uuid: 'c1', hasApiKey: true }
    );
    expect(r).toEqual([{ id: 'm' }]);
    expect(api.getOpenAICompatibleModels).toHaveBeenCalledWith('u', undefined, 'c1');
  });
});

describe('discoverAnthropicModels', () => {
  it('needs a typed or stored key and sends only a typed one', async () => {
    expect(await discoverAnthropicModels(mk(), ' ', null)).toBeNull();
    expect(toast.error).toHaveBeenCalledWith('llm.enterApiKeyFirst');
    api.getAnthropicModels.mockResolvedValue({ success: true, models: [{ id: 'c' }], message: '' });
    await discoverAnthropicModels(mk(), '', { uuid: 'c1', hasApiKey: true });
    expect(api.getAnthropicModels).toHaveBeenCalledWith(undefined, 'c1');
  });
});
