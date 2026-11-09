import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ fetchSummaryData: vi.fn(), startSummary: vi.fn() }));
vi.mock('$lib/fileDetail/summaryActions', () => mocks);

const llm = vi.hoisted(() => ({ available: null as null | { set: (v: boolean) => void } }));
vi.mock('$stores/llmStatus', async () => {
  const { writable: w } = await import('svelte/store');
  const store = w(true);
  llm.available = store;
  return { isLLMAvailable: store };
});

const mockToast = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$components/SummaryModal.svelte', async () => ({
  default: (await import('./SummaryModalTestStub.svelte')).default,
}));

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import FileSummaryTestHost from './FileSummaryTestHost.svelte';

type Api = { show: () => Promise<void>; generate: () => Promise<void> };
const FILE = { uuid: 'f1', filename: 'a.mp4' };

beforeEach(() => {
  vi.clearAllMocks();
  llm.available?.set(true);
  window.history.replaceState({}, '', '/files/f1');
});
afterEach(() => window.history.replaceState({}, '', '/'));

function setup(props: Record<string, unknown> = {}) {
  const result = render(FileSummaryTestHost, { props: { file: FILE, ...props } });
  return {
    ...result,
    api: result.component as unknown as Api,
    flags: () => result.getByTestId('flags').textContent,
    isOpen: () => result.queryByTestId('summary-open') !== null,
  };
}

describe('FileSummary', () => {
  it('loads the summary once, opens the modal, and reuses it', async () => {
    mocks.fetchSummaryData.mockResolvedValue({ bluf: 'x' });
    const { api, isOpen } = setup();

    await api.show();
    await waitFor(() => expect(isOpen()).toBe(true));
    await api.show();

    expect(mocks.fetchSummaryData).toHaveBeenCalledTimes(1);
    expect(mocks.fetchSummaryData).toHaveBeenCalledWith('f1');
  });

  it('stays closed and retries the load when there was no summary yet', async () => {
    mocks.fetchSummaryData.mockResolvedValue(undefined);
    const { api, isOpen } = setup();

    await api.show();
    await api.show();

    expect(isOpen()).toBe(false);
    expect(mocks.fetchSummaryData).toHaveBeenCalledTimes(2);
  });

  it('opens from ?view=summary once loading is done, and not before', async () => {
    window.history.replaceState({}, '', '/files/f1?view=summary&section=2');
    mocks.fetchSummaryData.mockResolvedValue({ bluf: 'x' });

    const { rerender, isOpen } = setup({ isLoading: true });
    expect(mocks.fetchSummaryData).not.toHaveBeenCalled();

    await rerender({ file: FILE, isLoading: false });
    await waitFor(() => expect(isOpen()).toBe(true));
    await rerender({ file: { ...FILE }, isLoading: false });
    expect(mocks.fetchSummaryData).toHaveBeenCalledTimes(1);
  });

  it('generate asks the server only with an LLM, and clears its spinner after a failure', async () => {
    const { api, flags } = setup();

    llm.available?.set(false);
    await api.generate();
    expect(mocks.startSummary).not.toHaveBeenCalled();

    llm.available?.set(true);
    mocks.startSummary.mockRejectedValueOnce(new Error('down'));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    await api.generate();
    expect(mocks.startSummary).toHaveBeenCalledWith('f1');
    expect(mockToast.error).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(flags()).toBe('false|false|undefined'));
  });

  it('a reprocess from the modal clears the summary and shows it as generating', async () => {
    mocks.startSummary.mockResolvedValue(undefined);
    const { getByTestId, flags } = setup({ file: { ...FILE, has_summary: true } });

    await fireEvent.click(getByTestId('reprocess-summary'));

    await waitFor(() => expect(flags()).toBe('true|false|false'));
    expect(mocks.startSummary).toHaveBeenCalledWith('f1', { forceRegenerate: true });
  });
});
