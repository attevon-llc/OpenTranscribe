/**
 * Runtime behaviour of the file page's export/copy flow, moved out of the route page into
 * `TranscriptExportDialogs.svelte`. Every path ends at the server (`requestTranscriptExport`
 * / `startDocumentExport`); these pin which path each format takes, what it sends, and that
 * a refusal is told to the user rather than answered locally.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';

const mockRequestTranscriptExport = vi.hoisted(() => vi.fn());
vi.mock('$lib/export/requestTranscriptExport', () => ({
  requestTranscriptExport: mockRequestTranscriptExport,
}));

const mockStartDocumentExport = vi.hoisted(() => vi.fn());
vi.mock('$lib/export/requestDocumentExport', () => ({
  startDocumentExport: mockStartDocumentExport,
  documentTemplateLabels: () => ({}),
}));

const mockTriggerAnchorDownload = vi.hoisted(() => vi.fn());
vi.mock('$lib/export/anchorDownload', () => ({ triggerAnchorDownload: mockTriggerAnchorDownload }));

const mockCopyToClipboard = vi.hoisted(() => vi.fn());
vi.mock('$lib/utils/clipboard', () => ({ copyToClipboard: mockCopyToClipboard }));

const mockToast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import TranscriptExportDialogsTestHost from './TranscriptExportDialogsTestHost.svelte';

const FILE = { uuid: 'file-1', filename: 'meeting.mp4' };

type Dialogs = {
  openExport: (format: string, hasComments: boolean) => Promise<void>;
  copyTranscript: () => Promise<void>;
};

function setup(props: Record<string, unknown> = {}) {
  const schedule = vi.fn();
  const result = render(TranscriptExportDialogsTestHost, {
    props: { file: FILE, schedule, ...props },
  });
  return {
    ...result,
    schedule,
    dialogs: result.component as unknown as Dialogs,
    copyStatus: () => result.getByTestId('copy-status').textContent,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  window.URL.createObjectURL = vi.fn(() => 'blob:x');
  window.URL.revokeObjectURL = vi.fn();
});

describe('TranscriptExportDialogs — formats', () => {
  it('exports a plain format straight away, without comments, when the file has none', async () => {
    mockRequestTranscriptExport.mockResolvedValue({
      data: 'x',
      contentType: 'application/json',
      filename: 'server.json',
    });
    const { dialogs } = setup({ showOriginal: true });

    await dialogs.openExport('json', false);

    await waitFor(() => expect(mockRequestTranscriptExport).toHaveBeenCalledTimes(1));
    expect(mockRequestTranscriptExport.mock.calls[0][0]).toMatchObject({
      fileUuid: 'file-1',
      format: 'json',
      includeComments: false,
      includeTimestamps: true,
      includeSpeakers: true,
      showOriginal: true,
    });
  });

  it('asks before including comments, and exports with them on confirm', async () => {
    mockRequestTranscriptExport.mockResolvedValue({ data: 'x', contentType: 'text/csv' });
    const { dialogs, getByText } = setup();

    await dialogs.openExport('csv', true);
    await tick();

    expect(mockRequestTranscriptExport).not.toHaveBeenCalled();
    await fireEvent.click(getByText('exportConfirm.includeComments'));

    await waitFor(() => expect(mockRequestTranscriptExport).toHaveBeenCalledTimes(1));
    expect(mockRequestTranscriptExport.mock.calls[0][0]).toMatchObject({
      format: 'csv',
      includeComments: true,
    });
  });

  it('opens the TXT options dialog instead of exporting, speakers forced off without diarization', async () => {
    mockRequestTranscriptExport.mockResolvedValue({ data: 'x', contentType: 'text/plain' });
    const { dialogs, getByText, container } = setup({ diarizationDisabled: true });

    await dialogs.openExport('txt', false);
    await tick();

    expect(mockRequestTranscriptExport).not.toHaveBeenCalled();
    getByText('exportOptions.title');
    const speakers =
      container.ownerDocument.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')[1];
    expect(speakers.checked).toBe(false);
  });

  it('toasts the translated reason when the server refuses an export', async () => {
    mockRequestTranscriptExport.mockRejectedValue({ response: { status: 409 } });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { dialogs } = setup();

    await dialogs.openExport('srt', false);

    await waitFor(() => expect(mockToast.error).toHaveBeenCalledTimes(1));
    expect(window.URL.createObjectURL).not.toHaveBeenCalled();
  });
});

describe('TranscriptExportDialogs — copy', () => {
  it('copies the server text and settles the status through the page schedule', async () => {
    mockRequestTranscriptExport.mockResolvedValue({ data: 'hello' });
    mockCopyToClipboard.mockImplementation(async (_text: string, onOk: () => void) => onOk());
    const { dialogs, schedule, copyStatus } = setup({ diarizationDisabled: true });

    await dialogs.copyTranscript();
    await waitFor(() => expect(copyStatus()).toBe('copied'));

    expect(mockRequestTranscriptExport.mock.calls[0][0]).toMatchObject({
      format: 'txt',
      includeSpeakers: false,
      responseType: 'text',
    });
    expect(mockCopyToClipboard.mock.calls[0][0]).toBe('hello');
    expect(schedule).toHaveBeenCalledWith(expect.any(Function), 2000);
  });

  it('never copies an empty export', async () => {
    mockRequestTranscriptExport.mockResolvedValue({ data: '   ' });
    const { dialogs, schedule, copyStatus } = setup();

    await dialogs.copyTranscript();

    await waitFor(() => expect(copyStatus()).toBe('empty'));
    expect(mockCopyToClipboard).not.toHaveBeenCalled();
    expect(schedule).toHaveBeenCalledTimes(1);
  });

  it('toasts and does not fall back to a local copy when the server refuses', async () => {
    mockRequestTranscriptExport.mockRejectedValue({ response: { status: 503 } });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { dialogs, copyStatus } = setup();

    await dialogs.copyTranscript();

    await waitFor(() => expect(copyStatus()).toBe('failed'));
    expect(mockToast.error).toHaveBeenCalledTimes(1);
    expect(mockCopyToClipboard).not.toHaveBeenCalled();
  });
});
