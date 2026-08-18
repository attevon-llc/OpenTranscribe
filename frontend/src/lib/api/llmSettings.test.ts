import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * `LLMSettingsApi` is mostly thin CRUD, so this file targets the three
 * methods with real logic: `getProviderDefaults` and `getProviderDisplayName`
 * both normalize a legacy `'claude'` alias before a map lookup, and
 * `getStatusDisplay` switches on a connection status to build an i18n'd
 * display object. A couple of representative CRUD calls are covered for
 * request-shape only.
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

// Identity translator so assertions can match on the i18n key rather than
// depending on the real locale copy.
vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
}));

import { LLMSettingsApi } from './llmSettings';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: {} });
  mockInstance.post.mockResolvedValue({ data: {} });
  mockInstance.put.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
});

describe('getProviderDefaults', () => {
  it('returns the anthropic defaults for the current provider value', () => {
    const defaults = LLMSettingsApi.getProviderDefaults('anthropic');
    expect(defaults.provider).toBe('anthropic');
    expect(defaults.model_name).toBe('claude-opus-4-5-20251101');
  });

  it('normalizes the legacy "claude" alias to anthropic before the lookup', () => {
    // Older saved configs / URLs may still carry the pre-rename provider value.
    const defaults = LLMSettingsApi.getProviderDefaults('claude');
    expect(defaults).toEqual(LLMSettingsApi.getProviderDefaults('anthropic'));
    expect(defaults.provider).toBe('anthropic');
  });

  it('falls back to an empty object for an unknown provider', () => {
    expect(LLMSettingsApi.getProviderDefaults('not-a-real-provider')).toEqual({});
  });
});

describe('getProviderDisplayName', () => {
  it('maps known providers to their display name', () => {
    expect(LLMSettingsApi.getProviderDisplayName('openai')).toBe('OpenAI');
    expect(LLMSettingsApi.getProviderDisplayName('anthropic')).toBe('Anthropic');
  });

  it('maps the legacy "claude" alias to the Anthropic display name', () => {
    expect(LLMSettingsApi.getProviderDisplayName('claude')).toBe('Anthropic');
  });

  it('falls back to echoing the raw provider string when unrecognized', () => {
    expect(LLMSettingsApi.getProviderDisplayName('mystery-provider')).toBe('mystery-provider');
  });
});

describe('getStatusDisplay', () => {
  it('renders the success case', () => {
    expect(LLMSettingsApi.getStatusDisplay('success')).toEqual({
      text: 'llm.status.connected',
      class: 'success',
      icon: '✓',
    });
  });

  it('renders the failed case', () => {
    expect(LLMSettingsApi.getStatusDisplay('failed')).toEqual({
      text: 'llm.status.failed',
      class: 'error',
      icon: '✗',
    });
  });

  it('renders the pending case', () => {
    expect(LLMSettingsApi.getStatusDisplay('pending')).toEqual({
      text: 'llm.status.testing',
      class: 'pending',
      icon: '...',
    });
  });

  it('renders untested explicitly', () => {
    expect(LLMSettingsApi.getStatusDisplay('untested')).toEqual({
      text: 'llm.status.untested',
      class: 'neutral',
      icon: '?',
    });
  });

  it('falls back to the untested display when status is undefined', () => {
    expect(LLMSettingsApi.getStatusDisplay(undefined)).toEqual({
      text: 'llm.status.untested',
      class: 'neutral',
      icon: '?',
    });
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
});
