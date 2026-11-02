import { describe, it, expect } from 'vitest';
import type { LocalEndpointStatus, ProviderDefaults } from '$lib/api/llmSettings';
import {
  SCHEMA_DEFAULT_MAX_TOKENS,
  awsFields,
  blankForm,
  buildPayload,
  formatModelSize,
  isConnectionTestValid,
  isFormValid,
  reconcileDiscovery,
  showsDiscovery,
  type LLMFormData,
} from './llmConfigForm';

const prov = (o: Partial<ProviderDefaults>) => o as ProviderDefaults;
const form = (o: Partial<LLMFormData>): LLMFormData => ({ ...blankForm(), ...o });
const ep = (reachable: boolean) => ({ reachable }) as LocalEndpointStatus;

describe('blankForm', () => {
  it('starts from the schema context window, not a bare literal', () => {
    expect(blankForm().max_tokens).toBe(SCHEMA_DEFAULT_MAX_TOKENS);
    expect(blankForm().provider).toBe('');
  });
});

describe('isFormValid', () => {
  const ok = form({ name: 'n', provider: 'openai', model_name: 'm', api_key: 'k' });
  const needsKey = prov({ requires_api_key: true });
  it('needs name, provider and model', () => {
    expect(isFormValid(ok, needsKey, false)).toBe(true);
    expect(isFormValid({ ...ok, name: ' ' }, needsKey, false)).toBe(false);
    expect(isFormValid({ ...ok, model_name: '' }, needsKey, false)).toBe(false);
    expect(isFormValid(ok, undefined, false)).toBe(false);
  });
  it('requires an API key only for a NEW configuration', () => {
    const noKey = { ...ok, api_key: '' };
    expect(isFormValid(noKey, needsKey, false)).toBe(false);
    expect(isFormValid(noKey, needsKey, true)).toBe(true);
  });
  it('requires a base URL where the provider supports one', () => {
    expect(isFormValid(ok, prov({ supports_custom_url: true }), false)).toBe(false);
    expect(
      isFormValid({ ...ok, base_url: 'http://x' }, prov({ supports_custom_url: true }), false)
    ).toBe(true);
  });
  it('rejects half an AWS key pair on bedrock only', () => {
    const half = form({ name: 'n', provider: 'bedrock', model_name: 'm', aws_access_key_id: 'AK' });
    expect(isFormValid(half, prov({}), false)).toBe(false);
    expect(isFormValid({ ...half, provider: 'openai' }, prov({}), false)).toBe(true);
  });
});

describe('isConnectionTestValid', () => {
  const needsKey = prov({ requires_api_key: true });
  it('lets an edit test with the stored key but a new config must type one', () => {
    const f = form({ provider: 'openai', model_name: 'm' });
    expect(isConnectionTestValid(f, needsKey, true)).toBe(true);
    expect(isConnectionTestValid(f, needsKey, false)).toBe(false);
    expect(isConnectionTestValid({ ...f, api_key: 'k' }, needsKey, false)).toBe(true);
  });
  it('ollama needs a base URL', () => {
    expect(
      isConnectionTestValid(form({ provider: 'ollama', model_name: 'm' }), prov({}), false)
    ).toBe(false);
  });
});

describe('payload building', () => {
  it('sends no AWS fields for a non-bedrock provider', () => {
    const p = buildPayload(form({ provider: 'openai', aws_access_key_id: 'AK' }), false);
    expect(Object.keys(p).some((k) => k.startsWith('aws_') || k === 'clear_aws_credentials')).toBe(
      false
    );
  });
  it('sends only non-blank, trimmed credentials for bedrock', () => {
    const f = form({
      provider: 'bedrock',
      aws_region: 'r',
      aws_access_key_id: ' AK ',
      aws_secret_access_key: ' ',
    });
    expect(awsFields(f)).toEqual({ aws_access_key_id: 'AK' });
    expect(buildPayload(f, false)).toMatchObject({ aws_region: 'r', aws_access_key_id: 'AK' });
  });
  it('clear_aws_credentials is sent only when editing', () => {
    const f = form({ provider: 'bedrock', clear_aws_credentials: true });
    expect(buildPayload(f, true)).toMatchObject({ clear_aws_credentials: true });
    expect(buildPayload(f, false)).not.toHaveProperty('clear_aws_credentials');
  });
});

describe('formatModelSize', () => {
  it('formats gigabytes and blanks zero', () => {
    expect(formatModelSize(0)).toBe('');
    expect(formatModelSize(1.5 * 1024 ** 3)).toBe('1.5GB');
  });
});

describe('discovery', () => {
  it('shows only for local-capable (or unchosen) providers with something to show', () => {
    expect(showsDiscovery('', true, [])).toBe(true);
    expect(showsDiscovery('openai', true, [])).toBe(false);
    expect(showsDiscovery('ollama', false, [])).toBe(false);
    expect(showsDiscovery('vllm', false, [ep(false)])).toBe(true);
  });
  it('auto-expands with no provider and a reachable endpoint; a user toggle wins until the provider changes', () => {
    const open = reconcileDiscovery({ expanded: false, touched: false, seenProvider: '' }, '', [
      ep(true),
    ]);
    expect(open.expanded).toBe(true);
    const userClosed = reconcileDiscovery(
      { expanded: false, touched: true, seenProvider: '' },
      '',
      [ep(true)]
    );
    expect(userClosed.expanded).toBe(false);
    const changed = reconcileDiscovery(
      { expanded: false, touched: true, seenProvider: '' },
      'ollama',
      [ep(true)]
    );
    expect(changed).toEqual({ expanded: false, touched: false, seenProvider: 'ollama' });
  });
});
