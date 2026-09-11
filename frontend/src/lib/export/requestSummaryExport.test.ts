import { describe, it, expect, vi, beforeEach } from 'vitest';

const get = vi.fn();
vi.mock('$lib/axios', () => ({ default: { get: (...args: unknown[]) => get(...args) } }));

import { requestSummaryExport } from './requestSummaryExport';

const LABELS = {
  title: 'AI Summary - meeting.mp4',
  executiveSummary: 'Executive Summary (BLUF)',
  briefSummary: 'Brief Summary',
  majorTopics: 'Major Topics Discussed',
  keyParticipants: 'Key participants: {participants}',
  importanceHigh: 'HIGH',
  importanceMedium: 'MED',
  importanceLow: 'LOW',
  actionItems: 'Action Items',
  owner: 'Owner',
  dueDate: 'Due',
  keyDecisions: 'Key Decisions',
  speakerAnalysis: 'Speaker Analysis',
  followUpItems: 'Follow-up Items',
  disclaimer: 'AI-generated summary - please verify important details.',
};

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue({
    data: '# AI Summary\n\n...',
    headers: { 'content-type': 'text/markdown' },
  });
});

describe('requestSummaryExport (issue #885)', () => {
  it('asks the server to serialize the summary', async () => {
    await requestSummaryExport({ fileUuid: 'file-uuid', labels: LABELS });

    expect(get).toHaveBeenCalledTimes(1);
    const [path] = get.mock.calls[0] as [string, unknown];
    expect(path).toBe('/files/file-uuid/summary/export');
  });

  it('defaults to format=md', async () => {
    await requestSummaryExport({ fileUuid: 'file-uuid', labels: LABELS });

    const params = (get.mock.calls[0][1] as { params: Record<string, unknown> }).params;
    expect(params.format).toBe('md');
  });

  it('forwards the resolved i18n labels in snake_case, keeping the backend translation-free', async () => {
    await requestSummaryExport({ fileUuid: 'file-uuid', labels: LABELS });

    const params = (get.mock.calls[0][1] as { params: Record<string, unknown> }).params;
    expect(params.title_label).toBe('AI Summary - meeting.mp4');
    expect(params.action_items_label).toBe('Action Items');
    expect(params.speaker_analysis_label).toBe('Speaker Analysis');
    expect(params.owner_label).toBe('Owner');
    expect(params.due_date_label).toBe('Due');
  });

  it('forwards the key-participants label with its {participants} placeholder intact', async () => {
    await requestSummaryExport({ fileUuid: 'file-uuid', labels: LABELS });

    const params = (get.mock.calls[0][1] as { params: Record<string, unknown> }).params;
    expect(params.key_participants_label).toBe('Key participants: {participants}');
  });

  it('requests text (not a blob)', async () => {
    await requestSummaryExport({ fileUuid: 'file-uuid', labels: LABELS });

    expect((get.mock.calls[0][1] as { responseType: string }).responseType).toBe('text');
  });

  it('returns the server payload and its content type', async () => {
    get.mockResolvedValue({
      data: '# AI Summary\n\n## Executive Summary (BLUF)\nhello',
      headers: { 'content-type': 'text/markdown; charset=utf-8' },
    });

    const result = await requestSummaryExport({ fileUuid: 'file-uuid', labels: LABELS });

    expect(result.data).toBe('# AI Summary\n\n## Executive Summary (BLUF)\nhello');
    expect(result.contentType).toBe('text/markdown; charset=utf-8');
  });

  it('propagates a rejection instead of falling back to anything local', async () => {
    get.mockRejectedValue(new Error('Request failed with status code 503'));

    await expect(requestSummaryExport({ fileUuid: 'file-uuid', labels: LABELS })).rejects.toThrow(
      '503'
    );
  });
});
