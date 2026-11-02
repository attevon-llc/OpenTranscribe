/**
 * The "Open AI settings" link in the summary dialog. The summary dialog is stacked above the
 * Settings dialog, so opening Settings from it left the AI settings page hidden behind the summary:
 * the link has to close the summary dialog as well.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

const modal = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock('$stores/settingsModalStore', () => ({ settingsModalStore: modal }));

vi.mock('../stores/llmStatus', async () => {
  const { writable } = await import('svelte/store');
  return { isLLMAvailable: writable(false) };
});

import Host from './SummaryModalTestHost.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockImplementation((url: string) => {
    if (url.includes('/summary-status')) {
      return Promise.resolve({
        data: {
          summary_status: 'pending',
          llm_available: false,
          can_retry: false,
          can_generate: true,
          summary_enabled_system: true,
          summary_enabled_user: true,
        },
      });
    }
    if (url.endsWith('/summary')) {
      return Promise.reject({ response: { status: 404, data: { detail: 'not found' } } });
    }
    return Promise.resolve({ data: { prompts: [] } });
  });
});

describe('SummaryModal: open AI settings', () => {
  it('opens the AI settings and closes the summary dialog', async () => {
    const onClose = vi.fn();
    render(Host, { props: { onClose } });

    await fireEvent.click(await screen.findByTestId('open-ai-settings'));

    expect(modal.open).toHaveBeenCalledWith('llm-provider');
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});
