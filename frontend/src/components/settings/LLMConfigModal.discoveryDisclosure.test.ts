/**
 * The local-server detection panel is a disclosure (owner feedback, issue #1245 round):
 *
 * - hidden entirely for providers that never run locally (openai, anthropic, openrouter,
 *   bedrock);
 * - for ollama / vllm / custom, COLLAPSED by default, with the result in its summary line;
 * - with no provider chosen yet, EXPANDED when at least one endpoint is reachable (that is
 *   when "Use this" helps) and collapsed otherwise;
 * - the Rescan button lives inside it; the toggle is a real button with aria-expanded.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import type { ProviderDefaults, LocalEndpointsResponse } from '../../lib/api/llmSettings';

const api = vi.hoisted(() => ({
  getLocalEndpoints: vi.fn(),
  createSettings: vi.fn(),
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

function provider(name: ProviderDefaults['provider'], over: Partial<ProviderDefaults> = {}) {
  return {
    provider: name,
    default_model: 'm',
    default_base_url: undefined,
    requires_api_key: false,
    supports_custom_url: false,
    max_context_length: undefined,
    description: name,
    ...over,
  } as ProviderDefaults;
}

const PROVIDERS = [
  provider('ollama', { supports_custom_url: true, default_base_url: 'http://localhost:11434' }),
  provider('vllm', { supports_custom_url: true, default_base_url: 'http://localhost:8000/v1' }),
  provider('custom', { supports_custom_url: true }),
  provider('openai', { requires_api_key: true, supports_custom_url: true }),
  provider('anthropic', { requires_api_key: true }),
  provider('openrouter', { requires_api_key: true, supports_custom_url: true }),
  provider('bedrock'),
];

function endpoints(reachable: number): LocalEndpointsResponse {
  const rows = [
    ['a', 'http://a:8000/v1', 'custom'],
    ['b', 'http://b:11434', 'ollama'],
    ['c', 'http://c:5199/v1', 'custom'],
  ] as const;
  return {
    endpoints: rows.map(([id, base_url, prov], i) => ({
      id,
      label: `Server ${id}`,
      base_url,
      provider: prov,
      reachable: i < reachable,
      models: i < reachable ? ['model-' + id] : [],
      start_command: `start ${id}`,
    })),
    private_endpoints_allowed: true,
  };
}

async function renderModal(reachable: number) {
  api.getLocalEndpoints.mockResolvedValue(endpoints(reachable));
  render(LLMConfigModal, {
    props: { show: true, editingConfig: null, supportedProviders: PROVIDERS },
  } as never);
  await waitFor(() => expect(api.getLocalEndpoints).toHaveBeenCalled());
}

async function pick(value: string) {
  await fireEvent.change(screen.getByLabelText('llm.provider') as HTMLSelectElement, {
    target: { value },
  });
}

const toggle = () => screen.getByRole('button', { name: /llm\.discovery\.title/ });

describe('LLMConfigModal — local detection disclosure', () => {
  it('is expanded with no provider chosen when something is reachable', async () => {
    await renderModal(2);

    const button = await waitFor(() => toggle());
    expect(button).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('http://a:8000/v1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'llm.discovery.rescan' })).toBeInTheDocument();
  });

  it('is collapsed with no provider chosen when nothing is reachable, and says so', async () => {
    await renderModal(0);

    const button = await waitFor(() => toggle());
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(button).toHaveTextContent('llm.discovery.summaryNone');
    expect(screen.queryByRole('button', { name: 'llm.discovery.rescan' })).not.toBeInTheDocument();
  });

  it.each(['ollama', 'vllm', 'custom'])(
    'is collapsed for %s and carries the running count in its summary',
    async (name) => {
      await renderModal(2);
      await waitFor(() => toggle());

      await pick(name);

      await waitFor(() => expect(toggle()).toHaveAttribute('aria-expanded', 'false'));
      expect(toggle()).toHaveTextContent('llm.discovery.summaryDetected:{"count":2}');
    }
  );

  it.each(['openai', 'anthropic', 'openrouter', 'bedrock'])(
    'is absent for %s, where local detection is irrelevant',
    async (name) => {
      await renderModal(2);
      await waitFor(() => toggle());

      await pick(name);

      await waitFor(() =>
        expect(
          screen.queryByRole('button', { name: /llm\.discovery\.title/ })
        ).not.toBeInTheDocument()
      );
      expect(screen.queryByText('llm.discovery.rescan')).not.toBeInTheDocument();
    }
  );

  it('opens by keyboard-operable button and shows Rescan inside, then keeps the user choice', async () => {
    await renderModal(1);
    await waitFor(() => toggle());
    await pick('ollama');
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-expanded', 'false'));

    await fireEvent.click(toggle());

    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
    const rescan = screen.getByRole('button', { name: 'llm.discovery.rescan' });
    expect(rescan).toBeInTheDocument();

    api.getLocalEndpoints.mockClear();
    await fireEvent.click(rescan);
    expect(api.getLocalEndpoints).toHaveBeenCalledTimes(1);
    // A rescan must not collapse a panel the user opened.
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-expanded', 'true'));
  });

  it('"Use this" still sets the provider and its URL, then leaves the panel collapsed', async () => {
    await renderModal(2);
    await waitFor(() => toggle());

    const row = screen.getByText('http://b:11434').closest('li')!;
    await fireEvent.click(row.querySelector('button.discovery-use-btn') as HTMLButtonElement);

    await waitFor(() =>
      expect((screen.getByLabelText('llm.provider') as HTMLSelectElement).value).toBe('ollama')
    );
    expect(((await screen.findByLabelText(/llm\.baseUrl/)) as HTMLInputElement).value).toBe(
      'http://b:11434'
    );
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-expanded', 'false'));
  });
});
