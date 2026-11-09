import { describe, it, expect, vi, beforeEach } from 'vitest';

const h = vi.hoisted(() => ({
  post: vi.fn(),
  llmKey: vi.fn(),
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
  axiosPost: vi.fn(),
  openStream: vi.fn(),
  download: vi.fn(),
}));
vi.mock('$lib/api/bulkActions', () => ({ postBulkAction: h.post }));
vi.mock('$lib/utils/bulkLlmOutcome', () => ({ bulkLlmFailureKey: h.llmKey }));
vi.mock('$stores/toast', () => ({ toastStore: h.toast }));
vi.mock('$lib/axios', () => ({ default: { post: h.axiosPost } }));
vi.mock('$lib/export/exportJobStream', () => ({ openExportJobStream: h.openStream }));
vi.mock('$lib/export/anchorDownload', () => ({ triggerAnchorDownload: h.download }));

import { BULK_ACTIONS, BulkExporter, eligibleFiles, runBulkAction } from './bulkActions';

const t = (key: string, o?: Record<string, unknown>) => (o ? `${key}:${JSON.stringify(o)}` : key);
const files = [
  { uuid: 'a', status: 'completed' },
  { uuid: 'b', status: 'error' },
  { uuid: 'c', status: 'processing' },
  { uuid: 'd', status: 'completed' },
] as never[];

beforeEach(() => {
  vi.clearAllMocks();
  h.llmKey.mockReturnValue(null);
});

describe('eligibleFiles', () => {
  it('keeps only selected files in an allowed status, in order', () => {
    expect(
      eligibleFiles(files, new Set(['a', 'b', 'd']), ['completed']).map((f) => f.uuid)
    ).toEqual(['a', 'd']);
  });
});

describe('runBulkAction', () => {
  it('does nothing for an empty selection', async () => {
    await runBulkAction(BULK_ACTIONS.summarize, files, new Set(), t);
    expect(h.post).not.toHaveBeenCalled();
    expect(h.toast.warning).not.toHaveBeenCalled();
  });

  it('warns and sends nothing when no selected file is eligible', async () => {
    await runBulkAction(BULK_ACTIONS.retry, files, new Set(['a']), t);
    expect(h.post).not.toHaveBeenCalled();
    expect(h.toast.warning).toHaveBeenCalledWith('gallery.bulk.noFailedFiles');
  });

  it('sends one request for the eligible files and toasts the split outcome', async () => {
    h.post.mockResolvedValue([{ success: true }, { success: false }, { success: true }]);
    await runBulkAction(BULK_ACTIONS.redact, files, new Set(['a', 'b', 'd']), t);
    expect(h.post).toHaveBeenCalledWith({ file_uuids: ['a', 'd'], action: 'redact' });
    expect(h.toast.success).toHaveBeenCalledWith('gallery.bulk.redactStarted:{"count":2}');
    expect(h.toast.error).toHaveBeenCalledWith('gallery.bulk.redactFailed:{"count":1}');
  });

  it('shows the LLM configuration failure instead of counts for LLM-backed actions only', async () => {
    h.post.mockResolvedValue([{ success: false }]);
    h.llmKey.mockReturnValue('gallery.bulk.llmUnusable');
    await runBulkAction(BULK_ACTIONS.summarize, files, new Set(['a']), t);
    expect(h.toast.error).toHaveBeenCalledTimes(1);
    expect(h.toast.error).toHaveBeenCalledWith('gallery.bulk.llmUnusable');
    h.toast.error.mockClear();
    await runBulkAction(BULK_ACTIONS.redact, files, new Set(['a']), t);
    expect(h.toast.error).toHaveBeenCalledWith('gallery.bulk.redactFailed:{"count":1}');
  });

  it('uses the server message on a thrown request for serverError actions, the generic line otherwise', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    h.post.mockRejectedValue({ response: { data: { detail: 'quota exceeded' } } });
    await runBulkAction(BULK_ACTIONS.summarize, files, new Set(['a']), t);
    expect(h.toast.error).toHaveBeenLastCalledWith('quota exceeded');
    await runBulkAction(BULK_ACTIONS.cancel, files, new Set(['c']), t);
    expect(h.toast.error).toHaveBeenLastCalledWith('gallery.bulk.cancelFailed:{"count":1}');
  });
});

describe('BulkExporter', () => {
  it('prepares a job for the completed selection and downloads the finished ZIP', async () => {
    h.axiosPost.mockResolvedValue({ data: { job_id: 'j1' } });
    h.openStream.mockReturnValue({
      result: Promise.resolve({ url: 'u', filename: 'f.zip', exported: 2, skipped: 1 }),
      close: vi.fn(),
    });
    await new BulkExporter().run('webvtt', files, new Set(['a', 'd', 'b']), t);
    expect(h.axiosPost).toHaveBeenCalledWith('/files/bulk-export/prepare', {
      file_uuids: ['a', 'd'],
      subtitle_format: 'webvtt',
      include_speakers: true,
    });
    expect(h.download).toHaveBeenCalledWith('u', 'f.zip');
    expect(h.toast.info).toHaveBeenCalledWith(
      'gallery.bulk.exportStarted:{"count":2,"format":"VTT"}'
    );
    expect(h.toast.warning).toHaveBeenCalledWith('gallery.bulk.exportSkipped:{"count":1}');
  });

  it('closes the previous stream when a newer export starts', async () => {
    const close = vi.fn();
    h.axiosPost.mockResolvedValue({ data: { job_id: 'j' } });
    h.openStream.mockReturnValue({ result: new Promise(() => {}), close });
    const exporter = new BulkExporter();
    void exporter.run('srt', files, new Set(['a']), t);
    await vi.waitFor(() => expect(h.openStream).toHaveBeenCalledTimes(1));
    void exporter.run('srt', files, new Set(['a']), t);
    await vi.waitFor(() => expect(close).toHaveBeenCalledTimes(1));
  });

  it('reports a failed start', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    h.axiosPost.mockRejectedValue(new Error('x'));
    await new BulkExporter().run('srt', files, new Set(['a']), t);
    expect(h.toast.error).toHaveBeenCalledWith('gallery.bulk.exportFailed:{"count":1}');
  });
});
