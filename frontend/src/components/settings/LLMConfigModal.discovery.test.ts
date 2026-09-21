/**
 * Issue #644: local-endpoint discovery in LLMConfigModal.
 *
 * The load-bearing regression this file exists to pin: "Use this" must set
 * `formData.provider` AND `formData.base_url` together, in an order that survives the
 * provider-select reactive block (issue #873's `$: if (formData.provider) {...}`),
 * whose `providerChanged` branch overwrites `base_url`/`model_name` from the catalog
 * defaults. Assigning `base_url` first — or assigning both in the same synchronous
 * tick without waiting for that reactive block to settle — gets silently wiped,
 * because Svelte batches reactive statements rather than running them inline with the
 * assignment that triggered them.
 *
 * Getting this wrong has TWO invisible consequences downstream
 * (`redaction.llm_guard.is_local_provider` gates both on `provider`): a mismatched
 * remote provider pointed at a local URL sends transcript excerpts through masking
 * for no reason, and throttles the user's own hourly chat quota for no reason. Neither
 * shows up in the UI, which is why this must be a unit test and not a visual check.
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
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import LLMConfigModal from './LLMConfigModal.svelte';

// `ollama` has a non-empty `default_base_url` (`http://localhost:11434`) — the
// provider most likely to have its discovery-filled URL clobbered, since the
// provider-select reactive block's "initial load" branch also fills an empty
// base_url from the catalog before the discovery override lands.
const OLLAMA_PROVIDER: ProviderDefaults = {
  provider: 'ollama',
  default_model: 'llama3.2:latest',
  default_base_url: 'http://localhost:11434',
  requires_api_key: false,
  supports_custom_url: true,
  max_context_length: 128000,
  description: 'Ollama',
};

const CUSTOM_PROVIDER: ProviderDefaults = {
  provider: 'custom',
  default_model: '',
  default_base_url: undefined,
  requires_api_key: false,
  supports_custom_url: true,
  max_context_length: undefined,
  description: 'Custom OpenAI-compatible',
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

function discoveryResponse(overrides?: Partial<LocalEndpointsResponse>): LocalEndpointsResponse {
  return {
    endpoints: [
      {
        id: 'llm-test-vllm',
        label: 'vLLM (--with-llm-test)',
        base_url: 'http://llm-test-vllm:8000/v1',
        provider: 'custom',
        reachable: true,
        models: ['gemma-4-e4b'],
        start_command: './opentr.sh start dev --with-llm-test',
      },
      {
        id: 'llm-test-ollama',
        label: 'Ollama (--with-llm-test, --profile ollama)',
        base_url: 'http://llm-test-ollama:11434',
        provider: 'ollama',
        reachable: true,
        models: ['gemma3:4b'],
        start_command:
          'docker compose -f docker-compose.yml -f docker-compose.llm-test.yml --profile ollama up -d llm-test-ollama',
      },
      {
        id: 'mock-llm',
        label: 'Mock LLM (--with-mock-llm)',
        base_url: 'http://mock-llm:5199/v1',
        provider: 'custom',
        reachable: false,
        models: [],
        start_command: './opentr.sh start dev --with-mock-llm',
      },
    ],
    private_endpoints_allowed: true,
    ...overrides,
  };
}

function renderModal(supportedProviders: ProviderDefaults[]) {
  return render(LLMConfigModal, {
    props: { show: true, editingConfig: null, supportedProviders },
  } as never);
}

describe('LLMConfigModal — local endpoint discovery (issue #644)', () => {
  it('renders the discovery panel above the base URL field, with no provider selected yet', async () => {
    api.getLocalEndpoints.mockResolvedValue(discoveryResponse());
    renderModal([OLLAMA_PROVIDER, CUSTOM_PROVIDER, OPENAI_PROVIDER]);

    await waitFor(() => {
      expect(screen.getByText('llm.discovery.title')).toBeInTheDocument();
    });

    // The base_url field only renders once a provider is picked — the discovery
    // section must not depend on that, or a user with no provider selected (exactly
    // who needs the feature) would never see it.
    expect(screen.queryByLabelText(/llm\.baseUrl/)).not.toBeInTheDocument();
    expect(screen.getByText('http://llm-test-vllm:8000/v1')).toBeInTheDocument();
  });

  it('"Use this" sets base_url AND provider together, surviving the provider-select reactive block', async () => {
    api.getLocalEndpoints.mockResolvedValue(discoveryResponse());
    renderModal([OLLAMA_PROVIDER, CUSTOM_PROVIDER, OPENAI_PROVIDER]);

    await waitFor(() => screen.getByText('llm.discovery.title'));

    const ollamaRow = screen.getByText('http://llm-test-ollama:11434').closest('li')!;
    const useButton = ollamaRow.querySelector('button.discovery-use-btn') as HTMLButtonElement;
    await fireEvent.click(useButton);

    // Give the component's `await tick()` a turn to flush.
    await waitFor(() => {
      const select = screen.getByLabelText('llm.provider') as HTMLSelectElement;
      expect(select.value).toBe('ollama');
    });

    const baseUrlInput = await screen.findByLabelText(/llm\.baseUrl/);
    // The regression this test exists to catch: without the tick()-ordering fix,
    // the provider-select reactive block's "initial load" branch fills the empty
    // base_url from ollama's OWN catalog default (`http://localhost:11434`) either
    // before or instead of the discovery value landing.
    expect((baseUrlInput as HTMLInputElement).value).toBe('http://llm-test-ollama:11434');
    expect((baseUrlInput as HTMLInputElement).value).not.toBe('http://localhost:11434');

    // Not `getByLabelText`: the ollama branch nests a "discover models" <button>
    // inside the `<label for="model-name">`, so the label text implicitly
    // associates with BOTH the button and the explicitly-`for`-linked input —
    // an ambiguity pre-dating this change, not something to route around with a
    // weaker assertion. `#model-name` is unambiguous.
    const modelInput = document.getElementById('model-name') as HTMLInputElement;
    expect(modelInput.value).toBe('gemma3:4b');
  });

  it('"Use this" on a `custom`-provider row does not leave base_url wiped to empty', async () => {
    // `custom`'s `default_base_url` is `undefined` — the provider most likely to
    // expose a bug that clears `base_url` back to '' rather than leaving a stale
    // localhost default, since the reactive block's fallback is falsy either way.
    api.getLocalEndpoints.mockResolvedValue(discoveryResponse());
    renderModal([OLLAMA_PROVIDER, CUSTOM_PROVIDER, OPENAI_PROVIDER]);

    await waitFor(() => screen.getByText('llm.discovery.title'));

    const vllmRow = screen.getByText('http://llm-test-vllm:8000/v1').closest('li')!;
    const useButton = vllmRow.querySelector('button.discovery-use-btn') as HTMLButtonElement;
    await fireEvent.click(useButton);

    await waitFor(() => {
      const select = screen.getByLabelText('llm.provider') as HTMLSelectElement;
      expect(select.value).toBe('custom');
    });

    const baseUrlInput = await screen.findByLabelText(/llm\.baseUrl/);
    expect((baseUrlInput as HTMLInputElement).value).toBe('http://llm-test-vllm:8000/v1');
  });

  it('switching FROM a provider selected via "Use this" to a different provider still applies that provider\'s own catalog default afterward', async () => {
    // Guards against the fix over-correcting into "base_url is permanently sticky" —
    // a later, ordinary provider change must still behave exactly as issue #873 wants.
    api.getLocalEndpoints.mockResolvedValue(discoveryResponse());
    renderModal([OLLAMA_PROVIDER, CUSTOM_PROVIDER, OPENAI_PROVIDER]);

    await waitFor(() => screen.getByText('llm.discovery.title'));

    const ollamaRow = screen.getByText('http://llm-test-ollama:11434').closest('li')!;
    await fireEvent.click(ollamaRow.querySelector('button.discovery-use-btn') as HTMLButtonElement);
    await waitFor(() => {
      expect((screen.getByLabelText('llm.provider') as HTMLSelectElement).value).toBe('ollama');
    });

    const select = screen.getByLabelText('llm.provider') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: 'openai' } });

    const baseUrlInput = await screen.findByLabelText(/llm\.baseUrl/);
    expect((baseUrlInput as HTMLInputElement).value).toBe('https://api.openai.com/v1');
  });

  it('shows the private-endpoints-disabled warning only when a row is reachable and the flag is false', async () => {
    api.getLocalEndpoints.mockResolvedValue(
      discoveryResponse({ private_endpoints_allowed: false })
    );
    renderModal([OLLAMA_PROVIDER, CUSTOM_PROVIDER, OPENAI_PROVIDER]);

    await waitFor(() => screen.getByText('llm.discovery.title'));
    expect(screen.getByText('llm.discovery.privateEndpointsDisabledWarning')).toBeInTheDocument();
  });

  it('does not show the warning when private endpoints are already allowed', async () => {
    api.getLocalEndpoints.mockResolvedValue(discoveryResponse({ private_endpoints_allowed: true }));
    renderModal([OLLAMA_PROVIDER, CUSTOM_PROVIDER, OPENAI_PROVIDER]);

    await waitFor(() => screen.getByText('llm.discovery.title'));
    expect(
      screen.queryByText('llm.discovery.privateEndpointsDisabledWarning')
    ).not.toBeInTheDocument();
  });

  it('renders no discovery section at all when the probe finds nothing (feature stays invisible)', async () => {
    api.getLocalEndpoints.mockResolvedValue({ endpoints: [], private_endpoints_allowed: false });
    renderModal([OLLAMA_PROVIDER, CUSTOM_PROVIDER, OPENAI_PROVIDER]);

    await waitFor(() => expect(api.getLocalEndpoints).toHaveBeenCalled());
    // The panel legitimately renders WHILE the probe is in flight (a brief spinner
    // state) — the real claim is that it disappears once the empty result settles.
    await waitFor(() => {
      expect(screen.queryByText('llm.discovery.title')).not.toBeInTheDocument();
    });
  });

  it('an unreachable row shows its start_command and no "Use this" button', async () => {
    api.getLocalEndpoints.mockResolvedValue(discoveryResponse());
    renderModal([OLLAMA_PROVIDER, CUSTOM_PROVIDER, OPENAI_PROVIDER]);

    await waitFor(() => screen.getByText('llm.discovery.title'));

    const mockRow = screen.getByText('./opentr.sh start dev --with-mock-llm').closest('li')!;
    expect(mockRow.querySelector('button.discovery-use-btn')).toBeNull();
  });

  it('a discovery load failure degrades to no section rather than throwing or toasting', async () => {
    api.getLocalEndpoints.mockRejectedValue(new Error('network error'));
    renderModal([OLLAMA_PROVIDER, CUSTOM_PROVIDER, OPENAI_PROVIDER]);

    await waitFor(() => expect(api.getLocalEndpoints).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.queryByText('llm.discovery.title')).not.toBeInTheDocument();
    });
  });
});
