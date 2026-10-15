/**
 * Bedrock configurations carry their own region and AWS credentials (owner decision, issue
 * #1245 round). The contract on the form side:
 *
 * - the credential values are WRITE-ONLY: never pre-filled when editing, only a "saved" chip;
 * - blank credential fields are not sent ("leave blank to keep it") and a stored key is only
 *   dropped by an explicit "remove" checkbox;
 * - nothing AWS-shaped is sent for any other provider;
 * - a plain user sees no server-credential lookup, an admin does;
 * - user-facing copy never names an environment variable (only the admin-detail string may).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ProviderDefaults, UserLLMSettings } from '../../lib/api/llmSettings';

const api = vi.hoisted(() => ({
  getLocalEndpoints: vi.fn(),
  getBedrockServerCredentials: vi.fn(),
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

vi.mock('../../stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

const authUser = vi.hoisted(() => ({
  store: null as null | { set: (v: { role: string } | null) => void },
}));
vi.mock('$stores/auth', async () => {
  const { writable: w } = await import('svelte/store');
  const store = w<{ role: string } | null>({ role: 'user' });
  authUser.store = store;
  return { user: store };
});

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => {
      run((key: string, params?: Record<string, unknown>) =>
        params ? `${key}:${JSON.stringify(params)}` : key
      );
      return () => {};
    },
  },
}));

import LLMConfigModal from './LLMConfigModal.svelte';

const KEY_ID = 'AKIAEXAMPLE0FAKE0KEY'; // gitleaks:allow - fabricated fixture
const SECRET = 'fakeSecretAccessKey0123456789ABCDEFGHIJ'; // gitleaks:allow - fabricated fixture

const BEDROCK: ProviderDefaults = {
  provider: 'bedrock',
  default_model: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  requires_api_key: false,
  supports_custom_url: false,
  description: 'AWS Bedrock',
  suggested_regions: ['us-east-1', 'us-west-2'],
  suggested_models: ['global.anthropic.claude-haiku-4-5-20251001-v1:0'],
};
const OPENAI: ProviderDefaults = {
  provider: 'openai',
  default_model: 'gpt-4o-mini',
  default_base_url: 'https://api.openai.com/v1',
  requires_api_key: true,
  supports_custom_url: true,
  max_context_length: 128000,
  description: 'OpenAI',
};

function setRole(role: string) {
  authUser.store?.set({ role });
}

function renderModal(editingConfig: UserLLMSettings | null = null) {
  return render(LLMConfigModal, {
    props: { show: true, editingConfig, supportedProviders: [OPENAI, BEDROCK] },
  } as never);
}

async function selectProvider(value: string) {
  await fireEvent.change(screen.getByLabelText('llm.provider') as HTMLSelectElement, {
    target: { value },
  });
}

function savedConfig(over: Partial<UserLLMSettings> = {}): UserLLMSettings {
  return {
    uuid: 'cfg-1',
    user_id: 'u-1',
    name: 'Saved Bedrock',
    provider: 'bedrock',
    model_name: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
    max_tokens: 200000,
    temperature: '0.3',
    is_active: true,
    has_api_key: false,
    aws_region: 'us-west-2',
    has_aws_credentials: true,
    created_at: '',
    updated_at: '',
    ...over,
  } as UserLLMSettings;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getLocalEndpoints.mockResolvedValue({ endpoints: [], private_endpoints_allowed: true });
  api.getBedrockServerCredentials.mockResolvedValue({
    found: true,
    source: 'instance_role',
    region: 'us-east-1',
  });
  setRole('user');
});

describe('Bedrock account fields', () => {
  it('renders region, access key, secret and token inputs, with the secrets as password fields', async () => {
    renderModal();
    await selectProvider('bedrock');

    expect(screen.getByLabelText('llm.bedrock.region')).toBeInTheDocument();
    expect(screen.getByLabelText('llm.bedrock.accessKeyId')).toHaveAttribute('type', 'text');
    expect(screen.getByLabelText('llm.bedrock.secretAccessKey')).toHaveAttribute(
      'type',
      'password'
    );
    expect(screen.getByLabelText('llm.bedrock.sessionToken')).toHaveAttribute('type', 'password');
  });

  it('offers the catalog regions plus the server default', async () => {
    renderModal();
    await selectProvider('bedrock');

    const options = Array.from(
      (screen.getByLabelText('llm.bedrock.region') as HTMLSelectElement).options
    ).map((o) => o.value);
    expect(options).toEqual(['', 'us-east-1', 'us-west-2']);
  });

  it('suggests model ids through a datalist on the model field', async () => {
    renderModal();
    await selectProvider('bedrock');

    const model = document.getElementById('model-name') as HTMLInputElement;
    const list = document.getElementById(model.getAttribute('list') as string);
    expect(list?.querySelector('option')?.getAttribute('value')).toBe(
      'global.anthropic.claude-haiku-4-5-20251001-v1:0'
    );
  });

  it('a plain user sees "using the server" and triggers no server-credential lookup', async () => {
    setRole('user');
    renderModal();
    await selectProvider('bedrock');

    expect(screen.getByTestId('bedrock-credential-status')).toHaveTextContent(
      'llm.bedrock.statusServer'
    );
    expect(api.getBedrockServerCredentials).not.toHaveBeenCalled();
    expect(screen.queryByText(/statusServerFound/)).not.toBeInTheDocument();
  });

  it('an admin also sees whether the server credentials resolve, and from what kind of source', async () => {
    setRole('admin');
    renderModal();
    await selectProvider('bedrock');

    await waitFor(() =>
      expect(screen.getByTestId('bedrock-credential-status')).toHaveTextContent(
        'llm.bedrock.statusServerFound'
      )
    );
    expect(screen.getByTestId('bedrock-credential-status')).toHaveTextContent(
      'llm.bedrock.source.instanceRole'
    );
  });

  it('an admin is told when the server has no credentials', async () => {
    api.getBedrockServerCredentials.mockResolvedValue({ found: false, source: null, region: null });
    setRole('super_admin');
    renderModal();
    await selectProvider('bedrock');

    await waitFor(() =>
      expect(screen.getByTestId('bedrock-credential-status')).toHaveTextContent(
        'llm.bedrock.statusServerMissing'
      )
    );
  });

  it('the admin detail (which names the setting) is only in the tip for an admin', async () => {
    setRole('user');
    const { unmount } = renderModal();
    await selectProvider('bedrock');
    await fireEvent.click(screen.getByRole('button', { name: 'llm.bedrock.infoLabel' }));
    expect(screen.getByText('llm.bedrock.info')).toBeInTheDocument();
    expect(screen.queryByText('llm.bedrock.infoAdmin')).not.toBeInTheDocument();
    unmount();

    setRole('admin');
    renderModal();
    await selectProvider('bedrock');
    await fireEvent.click(screen.getByRole('button', { name: 'llm.bedrock.infoLabel' }));
    expect(screen.getByText('llm.bedrock.infoAdmin')).toBeInTheDocument();
  });

  it('a half-entered key pair disables Save and says why', async () => {
    renderModal();
    await selectProvider('bedrock');
    await fireEvent.input(screen.getByLabelText('llm.configName'), { target: { value: 'B' } });

    await fireEvent.input(screen.getByLabelText('llm.bedrock.accessKeyId'), {
      target: { value: KEY_ID },
    });

    expect(screen.getByRole('alert')).toHaveTextContent('llm.bedrock.pairRequired');
    expect(screen.getByRole('button', { name: /llm\.saveConfiguration/ })).toBeDisabled();
  });
});

describe('Bedrock credentials on the wire', () => {
  it('creating with an entered key sends region and the key pair, trimmed', async () => {
    api.createSettings.mockResolvedValue({ uuid: 'new' });
    renderModal();
    await selectProvider('bedrock');
    await fireEvent.input(screen.getByLabelText('llm.configName'), { target: { value: 'B' } });
    await fireEvent.change(screen.getByLabelText('llm.bedrock.region'), {
      target: { value: 'us-west-2' },
    });
    await fireEvent.input(screen.getByLabelText('llm.bedrock.accessKeyId'), {
      target: { value: `  ${KEY_ID} ` },
    });
    await fireEvent.input(screen.getByLabelText('llm.bedrock.secretAccessKey'), {
      target: { value: SECRET },
    });

    await fireEvent.click(screen.getByRole('button', { name: /llm\.saveConfiguration/ }));

    const payload = api.createSettings.mock.calls[0][0];
    expect(payload).toMatchObject({
      provider: 'bedrock',
      aws_region: 'us-west-2',
      aws_access_key_id: KEY_ID,
      aws_secret_access_key: SECRET,
    });
    expect(payload).not.toHaveProperty('aws_session_token');
    expect(payload).not.toHaveProperty('clear_aws_credentials');
  });

  it('blank credential fields are not sent at all (server credentials)', async () => {
    api.createSettings.mockResolvedValue({ uuid: 'new' });
    renderModal();
    await selectProvider('bedrock');
    await fireEvent.input(screen.getByLabelText('llm.configName'), { target: { value: 'B' } });

    await fireEvent.click(screen.getByRole('button', { name: /llm\.saveConfiguration/ }));

    const payload = api.createSettings.mock.calls[0][0];
    expect(payload).not.toHaveProperty('aws_access_key_id');
    expect(payload).not.toHaveProperty('aws_secret_access_key');
    expect(payload).not.toHaveProperty('aws_session_token');
  });

  it('no other provider sends any AWS field', async () => {
    api.createSettings.mockResolvedValue({ uuid: 'new' });
    renderModal();
    await selectProvider('openai');
    await fireEvent.input(screen.getByLabelText('llm.configName'), { target: { value: 'O' } });
    await fireEvent.input(document.getElementById('api-key') as HTMLInputElement, {
      target: { value: 'sk-test-fixture' },
    });

    await fireEvent.click(screen.getByRole('button', { name: /llm\.saveConfiguration/ }));

    const payload = api.createSettings.mock.calls[0][0];
    expect(Object.keys(payload).filter((k) => k.startsWith('aws_') || k.includes('aws'))).toEqual(
      []
    );
  });

  it('editing never pre-fills a credential, shows the saved chip and keeps it when left blank', async () => {
    api.updateSettings.mockResolvedValue({ uuid: 'cfg-1' });
    renderModal(savedConfig());

    await waitFor(() =>
      expect(screen.getByTestId('bedrock-credential-status')).toHaveTextContent(
        'llm.bedrock.statusSaved'
      )
    );
    expect((screen.getByLabelText('llm.bedrock.accessKeyId') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('llm.bedrock.secretAccessKey') as HTMLInputElement).value).toBe(
      ''
    );
    expect((screen.getByLabelText('llm.bedrock.region') as HTMLSelectElement).value).toBe(
      'us-west-2'
    );
    expect(api.getConfigApiKey).not.toHaveBeenCalled();

    await fireEvent.input(screen.getByLabelText('llm.configName'), {
      target: { value: 'Renamed' },
    });
    await fireEvent.click(screen.getByRole('button', { name: /llm\.updateConfiguration/ }));

    const payload = api.updateSettings.mock.calls[0][1];
    expect(payload).not.toHaveProperty('aws_access_key_id');
    expect(payload).not.toHaveProperty('aws_secret_access_key');
    expect(payload).not.toHaveProperty('clear_aws_credentials');
  });

  it('the remove checkbox sends clear_aws_credentials and nothing else about the key', async () => {
    api.updateSettings.mockResolvedValue({ uuid: 'cfg-1' });
    renderModal(savedConfig());

    await fireEvent.click(await screen.findByLabelText('llm.bedrock.removeCredentials'));
    await waitFor(() =>
      expect(screen.getByTestId('bedrock-credential-status')).toHaveTextContent(
        'llm.bedrock.statusServer'
      )
    );
    await fireEvent.click(screen.getByRole('button', { name: /llm\.updateConfiguration/ }));

    const payload = api.updateSettings.mock.calls[0][1];
    expect(payload.clear_aws_credentials).toBe(true);
    expect(payload).not.toHaveProperty('aws_secret_access_key');
  });

  it('entering a new key on an edit replaces the saved one and locks the remove checkbox', async () => {
    renderModal(savedConfig());
    await fireEvent.input(screen.getByLabelText('llm.bedrock.accessKeyId'), {
      target: { value: KEY_ID },
    });

    expect(screen.getByLabelText('llm.bedrock.removeCredentials')).toBeDisabled();
    expect(screen.getByTestId('bedrock-credential-status')).toHaveTextContent(
      'llm.bedrock.statusEntered'
    );
  });

  it('Test connection sends the entered, unsaved credentials and the stored config id', async () => {
    api.testConnection.mockResolvedValue({ success: true, status: 'success', message: 'ok' });
    renderModal(savedConfig());
    await fireEvent.input(screen.getByLabelText('llm.bedrock.accessKeyId'), {
      target: { value: KEY_ID },
    });
    await fireEvent.input(screen.getByLabelText('llm.bedrock.secretAccessKey'), {
      target: { value: SECRET },
    });

    await fireEvent.click(screen.getByRole('button', { name: /llm\.testConnection/ }));

    const request = api.testConnection.mock.calls[0][0];
    expect(request).toMatchObject({
      provider: 'bedrock',
      aws_region: 'us-west-2',
      aws_access_key_id: KEY_ID,
      aws_secret_access_key: SECRET,
      config_id: 'cfg-1',
    });
  });
});

describe('Bedrock copy', () => {
  const en = JSON.parse(
    readFileSync(resolve(__dirname, '../../lib/i18n/locales/en.json'), 'utf-8')
  ) as Record<string, string>;
  const ENV_VAR = /\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/;

  it('names an environment variable only in the admin-detail string', () => {
    const offenders = Object.entries(en)
      .filter(([key]) => key.startsWith('llm.bedrock.') || key === 'llm.discovery.title')
      .filter(([key, value]) => key !== 'llm.bedrock.infoAdmin' && ENV_VAR.test(value))
      .map(([key]) => key);
    expect(offenders).toEqual([]);
    expect(en['llm.bedrock.infoAdmin']).toMatch(ENV_VAR);
  });

  it('the old "configured by your administrator" string is gone from every locale', () => {
    const dir = resolve(__dirname, '../../lib/i18n/locales');
    for (const lang of ['ar', 'de', 'en', 'es', 'fr', 'it', 'ja', 'ko', 'nl', 'pt', 'ru', 'zh']) {
      const locale = JSON.parse(readFileSync(resolve(dir, `${lang}.json`), 'utf-8'));
      expect(locale).not.toHaveProperty('llm.bedrockInfo');
      expect(locale).toHaveProperty('llm.bedrock.title');
    }
  });
});
