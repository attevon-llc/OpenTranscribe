import { describe, it, expect, vi, beforeEach } from 'vitest';

const get = vi.fn();
vi.mock('$lib/axios', () => ({ default: { get: (...args: unknown[]) => get(...args) } }));

import { requestTranscriptExport } from './requestTranscriptExport';

const LABELS = {
  speaker_default_label: 'Speaker',
  user_comment_label: 'USER COMMENT',
  comment_type_label: 'COMMENT',
  csv_header_default: 'Start,End,Speaker,Text',
  csv_header_with_comments: 'Start,End,Speaker,Text,Comment',
};

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue({ data: 'the transcript', headers: { 'content-type': 'text/plain' } });
});

describe('requestTranscriptExport (issues #673, #821)', () => {
  it('asks the SERVER to serialize, so the admin export_locked floor is consulted', async () => {
    await requestTranscriptExport({ fileUuid: 'file-uuid', format: 'txt', labels: LABELS });

    expect(get).toHaveBeenCalledTimes(1);
    const [path, config] = get.mock.calls[0] as [string, { params: Record<string, unknown> }];
    expect(path).toBe('/files/file-uuid/export');
    expect(config.params.format).toBe('txt');
  });

  it('omits `redact` entirely unless the reader has revealed the original', async () => {
    await requestTranscriptExport({ fileUuid: 'file-uuid', format: 'txt', labels: LABELS });

    const params = (get.mock.calls[0][1] as { params: Record<string, unknown> }).params;
    expect('redact' in params).toBe(false);
  });

  it('passes `redact: false` when the reader is viewing the original', async () => {
    await requestTranscriptExport({
      fileUuid: 'file-uuid',
      format: 'txt',
      labels: LABELS,
      showOriginal: true,
    });

    const params = (get.mock.calls[0][1] as { params: Record<string, unknown> }).params;
    // The SERVER still refuses this under export_locked — see
    // files/transcript_export._resolve_export_redaction. Sending it is a request, not a
    // decision, which is the whole difference between this and a client-side serializer.
    expect(params.redact).toBe(false);
  });

  it('forwards the resolved i18n labels, keeping the backend translation-free', async () => {
    await requestTranscriptExport({ fileUuid: 'file-uuid', format: 'csv', labels: LABELS });

    const params = (get.mock.calls[0][1] as { params: Record<string, unknown> }).params;
    expect(params.speaker_default_label).toBe('Speaker');
    expect(params.csv_header_with_comments).toBe('Start,End,Speaker,Text,Comment');
  });

  it('forwards the TXT timestamp/speaker toggles, defaulting both on', async () => {
    await requestTranscriptExport({ fileUuid: 'file-uuid', format: 'txt', labels: LABELS });
    let params = (get.mock.calls[0][1] as { params: Record<string, unknown> }).params;
    expect(params.include_timestamps).toBe(true);
    expect(params.include_speakers).toBe(true);

    get.mockClear();
    await requestTranscriptExport({
      fileUuid: 'file-uuid',
      format: 'txt',
      labels: LABELS,
      includeTimestamps: false,
      includeSpeakers: false,
    });
    params = (get.mock.calls[0][1] as { params: Record<string, unknown> }).params;
    expect(params.include_timestamps).toBe(false);
    expect(params.include_speakers).toBe(false);
  });

  it('requests text (not a blob) when the caller wants a string', async () => {
    await requestTranscriptExport({
      fileUuid: 'file-uuid',
      format: 'txt',
      labels: LABELS,
      responseType: 'text',
    });

    expect((get.mock.calls[0][1] as { responseType: string }).responseType).toBe('text');
  });

  it('returns the server payload and its content type', async () => {
    get.mockResolvedValue({
      data: 'SPEAKER_00:\nhello',
      headers: { 'content-type': 'text/plain; charset=utf-8' },
    });

    const result = await requestTranscriptExport({
      fileUuid: 'file-uuid',
      format: 'txt',
      labels: LABELS,
      responseType: 'text',
    });

    expect(result.data).toBe('SPEAKER_00:\nhello');
    expect(result.contentType).toBe('text/plain; charset=utf-8');
  });

  it('propagates a refusal instead of falling back to anything local', async () => {
    // A 503 (policy unresolvable) or 409 (redaction scan unfinished) must surface. A
    // client-side fallback here would reinstate exactly the bypass #821 closed.
    get.mockRejectedValue(new Error('Request failed with status code 503'));

    await expect(
      requestTranscriptExport({ fileUuid: 'file-uuid', format: 'txt', labels: LABELS })
    ).rejects.toThrow('503');
  });
});
