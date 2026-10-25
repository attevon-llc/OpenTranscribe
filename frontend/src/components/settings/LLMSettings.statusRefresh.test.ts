/**
 * The owner's report: files uploaded with no LLM provider, then a provider added in Settings,
 * and the file page's "Generate summary" button stayed disabled until a page refresh.
 *
 * Two defects made that happen (docs/design/llm_added_regenerate_plan.md): opening this panel
 * with no configuration called `llmStatusStore.reset()`, which also stopped the store's
 * re-check for the rest of the tab, and saving a configuration (which the server auto-activates
 * when it is the first) never refreshed the store at all. These tests drive the real panel and
 * the real config modal, then read the button the file page renders from `$isLLMAvailable`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import type { ProviderDefaults, UserLLMSettings } from '$lib/api/llmSettings';
import type { MediaFileDetail } from '$lib/types/media';

const server = vi.hoisted(() => ({
  configs: [] as Array<Record<string, unknown>>,
  activeId: null as string | null,
}));

const mockGetStatus = vi.hoisted(() => vi.fn());
vi.mock('$lib/services/llmService', () => ({
  llmService: { getStatus: mockGetStatus },
}));

const api = vi.hoisted(() => ({
  getSupportedProviders: vi.fn(),
  getUserConfigurations: vi.fn(),
  getLocalEndpoints: vi.fn(),
  createSettings: vi.fn(),
  deleteConfiguration: vi.fn(),
  testConfiguration: vi.fn(),
  testCurrentSettings: vi.fn(),
}));
vi.mock('$lib/api/llmSettings', async () => {
  const actual =
    await vi.importActual<typeof import('$lib/api/llmSettings')>('$lib/api/llmSettings');
  return { ...actual, LLMSettingsApi: { ...actual.LLMSettingsApi, ...api } };
});

vi.mock('$lib/axios', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: { ai_summary_enabled: true } }),
    put: vi.fn().mockResolvedValue({ data: { ai_summary_enabled: true } }),
  },
}));

vi.mock('$stores/toast', () => ({
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

import LLMSettings from './LLMSettings.svelte';
import FileActionButtons from '$components/fileDetail/FileActionButtons.svelte';
import { isLLMAvailable, llmStatusStore } from '$stores/llmStatus';

const OPENAI: ProviderDefaults = {
  provider: 'openai',
  default_model: 'gpt-4o-mini',
  default_base_url: 'https://api.openai.com/v1',
  requires_api_key: true,
  supports_custom_url: true,
  max_context_length: 128000,
  description: 'OpenAI',
};

const SAVED = {
  uuid: 'cfg-1',
  name: 'My OpenAI',
  provider: 'openai',
  model_name: 'gpt-4o-mini',
  base_url: 'https://api.openai.com/v1',
  is_shared: false,
  test_status: 'untested',
} as unknown as UserLLMSettings;

const completedFile = {
  uuid: 'f1',
  filename: 'a.mp4',
  upload_time: '2026-01-01T00:00:00Z',
  status: 'completed',
  transcript_segments: [{ uuid: 's1' }],
  has_summary: false,
} as unknown as MediaFileDetail;

function status(available: boolean) {
  return {
    available,
    user_id: '1',
    provider: available ? 'openai' : null,
    model: null,
    message: '',
  };
}

/** The file page passes `llmAvailable = $isLLMAvailable` to this component; mirror that. */
function renderGenerateButton() {
  const view = render(FileActionButtons, {
    props: { file: completedFile, canEdit: true, llmAvailable: false },
  });
  const unsubscribe = isLLMAvailable.subscribe((llmAvailable) => {
    void view.rerender({ file: completedFile, canEdit: true, llmAvailable });
  });
  const button = () => view.container.querySelector('.generate-summary-btn') as HTMLButtonElement;
  return { button, unsubscribe };
}

beforeEach(() => {
  vi.clearAllMocks();
  llmStatusStore.reset();
  server.configs = [];
  server.activeId = null;
  // The status endpoint answers from the server's state, like the real one.
  mockGetStatus.mockImplementation(async () => status(server.activeId !== null));
  api.getSupportedProviders.mockResolvedValue({ providers: [OPENAI] });
  api.getUserConfigurations.mockImplementation(async () => ({
    configurations: server.configs,
    shared_configurations: [],
    active_configuration_id: server.activeId,
    total: server.configs.length,
  }));
  api.getLocalEndpoints.mockResolvedValue({ endpoints: [], private_endpoints_allowed: true });
  api.createSettings.mockImplementation(async () => {
    // The backend auto-activates the first configuration (llm_settings.py).
    server.configs = [{ ...SAVED }];
    server.activeId = SAVED.uuid;
    return { ...SAVED };
  });
  api.deleteConfiguration.mockImplementation(async (uuid: string) => {
    server.configs = server.configs.filter((c) => c.uuid !== uuid);
    if (server.activeId === uuid) server.activeId = null;
    return { detail: 'deleted' };
  });
});

afterEach(() => {
  llmStatusStore.reset();
});

describe('LLMSettings keeps the LLM status store current', () => {
  it('enables Generate summary as soon as the first provider is saved, without a reload', async () => {
    await llmStatusStore.initialize(); // what the root layout does after sign-in
    const { button, unsubscribe } = renderGenerateButton();
    expect(button().disabled).toBe(true);

    render(LLMSettings, { props: { isAdmin: false } });
    await fireEvent.click(await screen.findByText('settings.llmProvider.createFirstConfig'));

    await fireEvent.change(screen.getByLabelText('llm.provider'), { target: { value: 'openai' } });
    await fireEvent.input(screen.getByLabelText('llm.configName'), {
      target: { value: 'My OpenAI' },
    });
    await fireEvent.input(screen.getByLabelText(/llm\.apiKey/), { target: { value: 'sk-test' } });
    await fireEvent.click(screen.getByRole('button', { name: /llm\.saveConfiguration/ }));

    expect(api.createSettings).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(button().disabled).toBe(false));
    expect(button().getAttribute('title')).not.toBe('fileDetail.aiNotAvailable');
    unsubscribe();
  });

  it('disables Generate summary again when the last provider is deleted', async () => {
    server.configs = [{ ...SAVED }];
    server.activeId = SAVED.uuid;
    await llmStatusStore.initialize();
    const { button, unsubscribe } = renderGenerateButton();
    await waitFor(() => expect(button().disabled).toBe(false));

    render(LLMSettings, { props: { isAdmin: false } });
    const del = await screen.findByTitle(`settings.llmProvider.deleteConfig ${SAVED.name}`);
    await fireEvent.click(del);
    await fireEvent.click(await screen.findByRole('button', { name: 'common.delete' }));

    expect(api.deleteConfiguration).toHaveBeenCalledWith(SAVED.uuid);
    await waitFor(() => expect(button().disabled).toBe(true));
    unsubscribe();
  });

  it('asks the server instead of assuming "unavailable" when the user has no configuration', async () => {
    // A system-level provider (LLM_PROVIDER) answers for a user with no configuration of their
    // own; opening this panel used to reset the store to "unavailable" regardless.
    mockGetStatus.mockImplementation(async () => status(true));
    await llmStatusStore.initialize();
    const { button, unsubscribe } = renderGenerateButton();
    await waitFor(() => expect(button().disabled).toBe(false));

    render(LLMSettings, { props: { isAdmin: false } });
    await screen.findByText('settings.llmProvider.createFirstConfig');
    await tick();

    expect(button().disabled).toBe(false);
    // ...and it re-asked rather than leaving the earlier answer in place by accident.
    await waitFor(() => expect(mockGetStatus).toHaveBeenCalledTimes(2));
    expect(get(llmStatusStore).lastChecked).toBeInstanceOf(Date);
    unsubscribe();
  });
});
