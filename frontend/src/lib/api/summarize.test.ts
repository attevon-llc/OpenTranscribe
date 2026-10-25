/**
 * `requestSummary` is the one summarize call the file page makes (Generate, Regenerate,
 * Regenerate with prompt), and `topicExtractionNoticeKey` decides which topic-extraction
 * outcomes the user is told about.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const post = vi.hoisted(() => vi.fn());
vi.mock('$lib/axios', () => ({ default: { post } }));

import { requestSummary, topicExtractionNoticeKey } from './summarize';

beforeEach(() => {
  post.mockReset();
  post.mockResolvedValue({
    data: { message: 'ok', task_id: 't', file_id: 'f', topic_extraction: 'queued' },
  });
});

describe('requestSummary', () => {
  it('sends no body for a plain generate, and returns the outcome', async () => {
    const res = await requestSummary('f1');
    expect(post).toHaveBeenCalledWith('/files/f1/summarize', undefined);
    expect(res.topic_extraction).toBe('queued');
  });

  it('sends force_regenerate and prompt_uuid when asked', async () => {
    await requestSummary('f1', { forceRegenerate: true, promptUuid: 'p1' });
    expect(post).toHaveBeenCalledWith('/files/f1/summarize', {
      force_regenerate: true,
      prompt_uuid: 'p1',
    });
  });
});

describe('topicExtractionNoticeKey', () => {
  it('names a message for queued and for a setting that is off', () => {
    expect(topicExtractionNoticeKey('queued')).toBe('fileDetail.topicSuggestionsQueued');
    expect(topicExtractionNoticeKey('disabled_by_setting')).toBe(
      'fileDetail.topicSuggestionsSkippedBySetting'
    );
  });

  it.each(['already_extracted', 'in_progress', 'unavailable', undefined] as const)(
    'says nothing for %s',
    (outcome) => {
      expect(topicExtractionNoticeKey(outcome)).toBeNull();
    }
  );
});
