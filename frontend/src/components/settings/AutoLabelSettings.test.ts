/**
 * The "suggest tags and collections automatically" switch (issue #1148): it renders, rides the
 * panel's existing save, and is disabled with an explanation when no AI provider is configured.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { LLMStatusState } from '$stores/llmStatus';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});

const llmState = writable<LLMStatusState>({
  status: null,
  available: true,
  checking: false,
  lastChecked: new Date(),
});
vi.mock('$stores/llmStatus', () => ({
  llmStatusStore: {
    subscribe: (run: (value: LLMStatusState) => void) => llmState.subscribe(run),
    initialize: vi.fn().mockResolvedValue(undefined),
  },
}));

const { setDirty } = vi.hoisted(() => ({ setDirty: vi.fn() }));
vi.mock('$stores/settingsModalStore', () => ({ settingsModalStore: { setDirty } }));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}),
  },
}));

import axiosInstance from '$lib/axios';
import AutoLabelSettings from './AutoLabelSettings.svelte';

const SERVER = {
  enabled: true,
  confidence_threshold: 0.75,
  tags_enabled: true,
  collections_enabled: true,
  bulk_grouping_enabled: true,
  auto_topic_extraction: true,
};

function mockServer(overrides: Record<string, unknown> = {}) {
  vi.mocked(axiosInstance.get).mockImplementation(async (url: string) => ({
    data: url.includes('retroactive') ? { running: false } : { ...SERVER, ...overrides },
  }));
}

describe('AutoLabelSettings: automatic suggestions switch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    llmState.set({ status: null, available: true, checking: false, lastChecked: new Date() });
    mockServer();
  });

  it('renders the switch with its plain-language label and help, on by default', async () => {
    render(AutoLabelSettings);
    const toggle = (await screen.findByLabelText('autoLabel.autoSuggestLabel')) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    expect(toggle.disabled).toBe(false);
    expect(screen.getByText('autoLabel.autoSuggestHelp')).toBeInTheDocument();
    expect(screen.queryByTestId('auto-suggest-no-provider')).toBeNull();
  });

  it('reflects an opted-out account', async () => {
    mockServer({ auto_topic_extraction: false });
    render(AutoLabelSettings);
    const toggle = (await screen.findByLabelText('autoLabel.autoSuggestLabel')) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
  });

  it('marks the panel dirty and saves the new value', async () => {
    vi.mocked(axiosInstance.put).mockResolvedValue({
      data: { ...SERVER, auto_topic_extraction: false },
    });
    render(AutoLabelSettings);
    await fireEvent.click(await screen.findByLabelText('autoLabel.autoSuggestLabel'));
    await waitFor(() => expect(setDirty).toHaveBeenLastCalledWith('auto-labeling', true));

    await fireEvent.click(screen.getByRole('button', { name: 'autoLabel.saveSettings' }));
    await waitFor(() => expect(axiosInstance.put).toHaveBeenCalledTimes(1));
    expect(axiosInstance.put).toHaveBeenCalledWith(
      '/user-settings/auto-label',
      expect.objectContaining({ auto_topic_extraction: false, enabled: true })
    );
    await waitFor(() => expect(setDirty).toHaveBeenLastCalledWith('auto-labeling', false));
  });

  it('is disabled with an explanation when no AI provider is configured', async () => {
    llmState.set({ status: null, available: false, checking: false, lastChecked: new Date() });
    render(AutoLabelSettings);
    const toggle = (await screen.findByLabelText('autoLabel.autoSuggestLabel')) as HTMLInputElement;
    expect(toggle.disabled).toBe(true);
    expect(screen.getByTestId('auto-suggest-no-provider')).toHaveTextContent(
      'autoLabel.autoSuggestNoProvider'
    );
  });

  it('does not grey the switch out before the provider check has run', async () => {
    llmState.set({ status: null, available: false, checking: true, lastChecked: null });
    render(AutoLabelSettings);
    const toggle = (await screen.findByLabelText('autoLabel.autoSuggestLabel')) as HTMLInputElement;
    expect(toggle.disabled).toBe(false);
  });
});
