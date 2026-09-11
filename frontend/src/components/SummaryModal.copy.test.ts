/**
 * `SummaryModal.svelte`'s copy button (issue #885).
 *
 * #885 was filed as a redaction bypass on the summary copy button; the premise was wrong —
 * `GET /files/{uuid}/summary` already masks correctly (issue #465). The real defects: the
 * modal's client-side markdown serializer dropped the action-items and speaker-analysis
 * sections, and it was the last client-side re-serialization of server data in the SPA. The
 * fix routes the copy through `requestSummaryExport` -> `GET /files/{uuid}/summary/export`,
 * same pattern `TranscriptModal.test.ts` already pins for the transcript's equivalent button.
 *
 * `$t` is left unmocked (as in `SummaryActions.test.ts`): i18next is uninitialised here, so
 * `$t('key')` returns the raw key, which lets these tests assert on button text without real
 * translations loaded.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

const requestSummaryExport = vi.hoisted(() => vi.fn());
vi.mock('$lib/export/requestSummaryExport', () => ({ requestSummaryExport }));

const copyToClipboard = vi.hoisted(() => vi.fn());
vi.mock('$lib/utils/clipboard', () => ({ copyToClipboard }));

// Avoids pulling in the real store's WebSocket-backed monitoring (same reasoning as
// TranscriptModal.test.ts mocking its store dependencies).
vi.mock('../stores/llmStatus', async () => {
  const { writable } = await import('svelte/store');
  return { isLLMAvailable: writable(true) };
});

import SummaryModal from './SummaryModal.svelte';

const SUMMARY_DATA = {
  bluf: 'Quarterly review.',
  brief_summary: 'The team reviewed Q3 numbers.',
  action_items: [{ item: 'File the report', owner: 'Priya' }],
  speakers_analysis: [
    { speaker: 'Priya', role: 'Finance lead', key_contributions: ['Presented budget'] },
  ],
};

function mockAxiosResponses() {
  mockAxios.get.mockImplementation((url: string) => {
    if (url.includes('/summary-status')) {
      return Promise.resolve({
        data: {
          summary_status: 'completed',
          llm_available: true,
          can_retry: false,
          can_generate: true,
          summary_enabled_system: true,
          summary_enabled_user: true,
        },
      });
    }
    if (url.endsWith('/summary')) {
      return Promise.resolve({
        data: { file_id: 'file-uuid', filename: 'meeting.mp4', summary_data: SUMMARY_DATA },
      });
    }
    // SummaryActions.svelte's onMount prompt fetch — irrelevant here.
    return Promise.resolve({ data: { prompts: [] } });
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAxiosResponses();
  requestSummaryExport.mockResolvedValue({
    data: '# AI Summary\n\nhello',
    contentType: 'text/markdown',
  });
  copyToClipboard.mockImplementation((_text: string, onSuccess?: () => void) => {
    onSuccess?.();
    return Promise.resolve({ success: true });
  });
});

async function renderOpenModal() {
  const utils = render(SummaryModal, {
    props: { fileId: 'file-uuid', fileName: 'meeting.mp4', isOpen: true },
  });
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'summary.copySummaryLabel' })).toBeInTheDocument()
  );
  return utils;
}

describe('SummaryModal copy button (issue #885)', () => {
  it('calls requestSummaryExport with the file uuid — the modal serializes nothing itself', async () => {
    await renderOpenModal();

    await fireEvent.click(screen.getByRole('button', { name: 'summary.copySummaryLabel' }));

    await waitFor(() => expect(requestSummaryExport).toHaveBeenCalledTimes(1));
    const call = requestSummaryExport.mock.calls[0][0];
    expect(call.fileUuid).toBe('file-uuid');
  });

  it('the clipboard receives the server response verbatim, not re-processed', async () => {
    requestSummaryExport.mockResolvedValue({
      data: '# AI Summary\n\n## Action Items\n- File the report',
      contentType: 'text/markdown',
    });
    await renderOpenModal();

    await fireEvent.click(screen.getByRole('button', { name: 'summary.copySummaryLabel' }));

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledTimes(1));
    expect(copyToClipboard.mock.calls[0][0]).toBe(
      '# AI Summary\n\n## Action Items\n- File the report'
    );
  });

  it('a server rejection is a refusal — the clipboard is never touched and the button shows failed', async () => {
    requestSummaryExport.mockRejectedValue(new Error('Request failed with status code 503'));
    await renderOpenModal();

    await fireEvent.click(screen.getByRole('button', { name: 'summary.copySummaryLabel' }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'summary.copySummaryLabel' })).toHaveTextContent(
        'summary.copyFailed'
      )
    );
    expect(copyToClipboard).not.toHaveBeenCalled();
  });

  it('disables the button while a copy is in flight', async () => {
    let resolveExport: (value: { data: string; contentType: string }) => void = () => {};
    requestSummaryExport.mockReturnValue(
      new Promise((resolve) => {
        resolveExport = resolve;
      })
    );
    await renderOpenModal();

    await fireEvent.click(screen.getByRole('button', { name: 'summary.copySummaryLabel' }));

    expect(screen.getByRole('button', { name: 'summary.copySummaryLabel' })).toBeDisabled();

    resolveExport({ data: 'hello', contentType: 'text/markdown' });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'summary.copySummaryLabel' })).not.toBeDisabled()
    );
  });

  it('shows the empty state when the server returns nothing to copy', async () => {
    requestSummaryExport.mockResolvedValue({ data: '   ', contentType: 'text/markdown' });
    await renderOpenModal();

    await fireEvent.click(screen.getByRole('button', { name: 'summary.copySummaryLabel' }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'summary.copySummaryLabel' })).toHaveTextContent(
        'summary.noContent'
      )
    );
    expect(copyToClipboard).not.toHaveBeenCalled();
  });
});
