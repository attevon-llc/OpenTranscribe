/**
 * What the buttons on a saved LLM configuration do: share it, test it, run the two probes,
 * activate another. The panel tracks which check is in flight (all probe buttons of that kind
 * are disabled meanwhile) and the shared-configurations list for a configuration someone else
 * owns offers only Activate and Test.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';
import type { ProviderDefaults, UserLLMSettings } from '$lib/api/llmSettings';

const api = vi.hoisted(() => ({
  getSupportedProviders: vi.fn(),
  getUserConfigurations: vi.fn(),
  testConfiguration: vi.fn(),
  probeReasoningCapability: vi.fn(),
  probeContextWindow: vi.fn(),
  toggleShare: vi.fn(),
  setActiveConfiguration: vi.fn(),
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

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

vi.mock('$lib/services/llmService', () => ({
  llmService: { getStatus: vi.fn().mockResolvedValue({ available: true }) },
}));

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import LLMSettings from './LLMSettings.svelte';

const OPENAI = {
  provider: 'openai',
  default_model: 'm',
  requires_api_key: true,
} as ProviderDefaults;
const MINE = {
  uuid: 'mine',
  name: 'Mine',
  provider: 'openai',
  model_name: 'm',
  is_shared: false,
  test_status: 'success',
  test_message: 'fine',
  last_tested: '2026-01-01T00:00:00Z',
} as unknown as UserLLMSettings;
const OTHER = { ...MINE, uuid: 'other', name: 'Other', test_status: 'untested' } as UserLLMSettings;
const THEIRS = {
  ...MINE,
  uuid: 'theirs',
  name: 'Theirs',
  owner_name: 'Dana',
  owner_role: 'admin',
} as UserLLMSettings;

beforeEach(() => {
  vi.clearAllMocks();
  api.getSupportedProviders.mockResolvedValue({ providers: [OPENAI] });
  api.getUserConfigurations.mockResolvedValue({
    configurations: [MINE, OTHER],
    shared_configurations: [THEIRS],
    active_configuration_id: 'mine',
    total: 2,
  });
  api.toggleShare.mockResolvedValue(undefined);
  api.setActiveConfiguration.mockResolvedValue(undefined);
  api.testCurrentSettings.mockResolvedValue({ success: true, message: 'ok' });
});

async function mount() {
  const view = render(LLMSettings, { props: { isAdmin: false } });
  await waitFor(() => expect(view.container.querySelector('.config-item')).not.toBeNull());
  return view;
}

const item = (c: HTMLElement, i: number) => c.querySelectorAll<HTMLElement>('.config-item')[i];
const byTestId = (el: HTMLElement, id: string) =>
  el.querySelector(`[data-testid="${id}"]`) as HTMLButtonElement;

describe('listing', () => {
  it('marks the active configuration and lists shared ones with their owner and badge', async () => {
    const { container } = await mount();
    expect(container.querySelectorAll('.config-item')).toHaveLength(3);
    expect(item(container, 0).classList.contains('active')).toBe(true);
    expect(item(container, 0).querySelector('.currently-active')?.getAttribute('title')).toBe(
      'fine'
    );
    expect(item(container, 2).classList.contains('shared')).toBe(true);
    expect(item(container, 2).querySelector('.admin-badge')).not.toBeNull();
    expect(item(container, 2).querySelector('.shared-by')?.textContent).toContain(
      'settings.llmProvider.sharedBy'
    );
    expect(item(container, 2).querySelector('.edit-button')).toBeNull();
    expect(item(container, 2).querySelector('.delete-config-button')).toBeNull();
  });
});

describe('sharing', () => {
  it('flips optimistically and keeps the new state on success', async () => {
    const { container } = await mount();
    await fireEvent.click(item(container, 1).querySelector('.toggle-input') as HTMLElement);
    await waitFor(() => expect(api.toggleShare).toHaveBeenCalledWith('other', true));
    await waitFor(() => expect(item(container, 1).querySelector('.share-badge')).not.toBeNull());
    expect(toast.success).toHaveBeenCalledWith('settings.llmProvider.shareEnabled', 3000);
  });

  it('puts the previous state back when the server refuses', async () => {
    api.toggleShare.mockRejectedValueOnce(new Error('nope'));
    const { container } = await mount();
    await fireEvent.click(item(container, 1).querySelector('.toggle-input') as HTMLElement);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('nope', 5000));
    expect(item(container, 1).querySelector('.share-badge')).toBeNull();
  });
});

describe('probes and tests', () => {
  it('runs the reasoning probe, disabling every reasoning-probe button while it runs', async () => {
    let finish!: (v: unknown) => void;
    api.probeReasoningCapability.mockReturnValueOnce(new Promise((r) => (finish = r)));
    const { container } = await mount();
    await fireEvent.click(byTestId(item(container, 0), 'llm-probe-reasoning'));
    await waitFor(() =>
      expect(byTestId(item(container, 1), 'llm-probe-reasoning').disabled).toBe(true)
    );
    expect(byTestId(item(container, 0), 'llm-probe-context-window').disabled).toBe(false);
    expect(toast.info).toHaveBeenCalledWith('settings.llmProvider.probeReasoningRunning', 6000);
    finish({ off_switch: 'works' });
    await waitFor(() =>
      expect(byTestId(item(container, 1), 'llm-probe-reasoning').disabled).toBe(false)
    );
    expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('Mine: '), 8000);
  });

  it('runs the context-window probe and warns on a mismatch', async () => {
    api.probeContextWindow.mockResolvedValueOnce({
      status: 'measured',
      relation: 'below',
      context_window: 100,
      configured_max_tokens: 8192,
    });
    const { container } = await mount();
    await fireEvent.click(byTestId(item(container, 1), 'llm-probe-context-window'));
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith(expect.stringContaining('Other: '), 10000)
    );
    expect(api.probeContextWindow).toHaveBeenCalledWith('other');
  });

  it('tests a saved configuration and disables the test buttons while it runs', async () => {
    let finish!: (v: unknown) => void;
    api.testConfiguration.mockReturnValueOnce(new Promise((r) => (finish = r)));
    const { container } = await mount();
    const testButton = (i: number) =>
      item(container, i).querySelectorAll<HTMLButtonElement>('.test-connection-button')[0];
    await fireEvent.click(testButton(2));
    await waitFor(() => expect(testButton(0).disabled).toBe(true));
    finish({ success: false, message: 'refused' });
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Theirs: refused', 8000));
    await waitFor(() => expect(testButton(0).disabled).toBe(false));
  });
});

describe('activating', () => {
  it('activates another configuration and re-checks its connection', async () => {
    const onSettingsChange = vi.fn();
    const view = render(LLMSettings, { props: { isAdmin: false, onSettingsChange } });
    await waitFor(() => expect(view.container.querySelector('.config-item')).not.toBeNull());
    await fireEvent.click(item(view.container, 1).querySelector('.activate-button') as HTMLElement);
    await waitFor(() => expect(api.setActiveConfiguration).toHaveBeenCalledWith('other'));
    await waitFor(() => expect(api.testCurrentSettings).toHaveBeenCalled());
    await waitFor(() => expect(item(view.container, 1).classList.contains('active')).toBe(true));
    expect(toast.success).toHaveBeenCalledWith('settings.llmProvider.configActivated', 5000);
    expect(onSettingsChange).toHaveBeenCalledTimes(1);
  });
});
