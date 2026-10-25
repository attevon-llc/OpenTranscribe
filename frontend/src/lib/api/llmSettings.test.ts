import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * `LLMSettingsApi` is mostly thin CRUD. `llmProviderDisplayName` (a top-level exported
 * function, not a class method) is the logic covered here; a couple of representative
 * CRUD calls are covered for request-shape only. (`getStatusDisplay`, which built a
 * text-glyph status icon, had no caller and was deleted.)
 */
const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('$lib/axios', async () => {
  const actual = await vi.importActual<typeof import('$lib/axios')>('$lib/axios');
  return { ...actual, default: mockInstance };
});

import { LLMSettingsApi, llmProviderDisplayName } from './llmSettings';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: {} });
  mockInstance.post.mockResolvedValue({ data: {} });
  mockInstance.put.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
});

describe('llmProviderDisplayName', () => {
  // Identity translator: assert on the i18n key itself rather than locale copy.
  const identity = (k: string) => k;

  it('maps a known provider to its i18n key', () => {
    expect(llmProviderDisplayName('openai', identity)).toBe('llm.provider.openai');
  });

  it('maps the legacy "claude" alias to its OWN key, distinct from anthropic', () => {
    // The live map intentionally does NOT collapse 'claude' into 'anthropic' —
    // a pre-rename stored config renders "Claude (Anthropic)", not "Anthropic".
    expect(llmProviderDisplayName('claude', identity)).toBe('llm.provider.claude');
    expect(llmProviderDisplayName('claude', identity)).not.toBe('llm.provider.anthropic');
  });

  it('falls back to echoing the raw provider string when unrecognized', () => {
    expect(llmProviderDisplayName('mystery-provider', identity)).toBe('mystery-provider');
  });

  it('maps bedrock to its i18n key', () => {
    expect(llmProviderDisplayName('bedrock', identity)).toBe('llm.provider.bedrock');
  });
});

describe('representative CRUD calls', () => {
  it('fetches supported providers from the providers endpoint', async () => {
    mockInstance.get.mockResolvedValue({ data: { providers: [] } });
    const result = await LLMSettingsApi.getSupportedProviders();
    expect(mockInstance.get).toHaveBeenCalledWith('/llm-settings/providers');
    expect(result).toEqual({ providers: [] });
  });

  it('creates a configuration by posting the settings payload', async () => {
    const payload = { name: 'My Config', provider: 'openai' as const, model_name: 'gpt-4o-mini' };
    mockInstance.post.mockResolvedValue({ data: { uuid: 'abc', ...payload } });
    const result = await LLMSettingsApi.createSettings(payload);
    expect(mockInstance.post).toHaveBeenCalledWith('/llm-settings', payload);
    expect(result).toEqual({ uuid: 'abc', ...payload });
  });

  it('deletes a specific configuration by id', async () => {
    mockInstance.delete.mockResolvedValue({ data: { detail: 'deleted' } });
    const result = await LLMSettingsApi.deleteConfiguration('config-uuid');
    expect(mockInstance.delete).toHaveBeenCalledWith('/llm-settings/config/config-uuid');
    expect(result).toEqual({ detail: 'deleted' });
  });

  it('sets the active configuration by posting the configuration id', async () => {
    mockInstance.post.mockResolvedValue({ data: { uuid: 'config-uuid', is_active: true } });
    const result = await LLMSettingsApi.setActiveConfiguration('config-uuid');
    expect(mockInstance.post).toHaveBeenCalledWith('/llm-settings/set-active', {
      configuration_id: 'config-uuid',
    });
    expect(result).toEqual({ uuid: 'config-uuid', is_active: true });
  });

  it('reads the server AWS credential status from the admin endpoint', async () => {
    const status = { found: true, source: 'instance_role', region: 'us-east-1' };
    mockInstance.get.mockResolvedValue({ data: status });
    const result = await LLMSettingsApi.getBedrockServerCredentials();
    expect(mockInstance.get).toHaveBeenCalledWith('/llm-settings/bedrock/server-credentials');
    expect(result).toEqual(status);
  });
});
