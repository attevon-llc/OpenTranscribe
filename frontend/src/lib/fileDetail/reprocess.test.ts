import { describe, it, expect } from 'vitest';
import {
  applyReprocessStart,
  isDestructiveReprocess,
  reprocessRegeneratesSummary,
} from './reprocess';

const FILE = {
  uuid: 'f1',
  status: 'completed',
  progress: 100,
  transcript_segments: [{ uuid: 's1' }],
  has_summary: true,
  summary_opensearch_id: 'os-1',
};

describe('applyReprocessStart', () => {
  it('full transcription: processing at 0% with the transcript and summary cleared', () => {
    const next = applyReprocessStart(FILE, ['transcription']);
    expect(next).toMatchObject({
      status: 'processing',
      progress: 0,
      transcript_segments: [],
      has_summary: false,
      summary_opensearch_id: null,
    });
    expect(FILE.status).toBe('completed');
  });

  it('re-diarization shows processing but keeps the transcript', () => {
    const next = applyReprocessStart(FILE, ['rediarize']);
    expect(next.status).toBe('processing');
    expect(next.transcript_segments).toEqual([{ uuid: 's1' }]);
    expect(next.has_summary).toBe(true);
  });

  it('a summary-only run, or no stages, leaves the file as it was', () => {
    expect(applyReprocessStart(FILE, ['summarization'])).toEqual(FILE);
    expect(applyReprocessStart(FILE, undefined)).toEqual(FILE);
  });
});

describe('isDestructiveReprocess / reprocessRegeneratesSummary', () => {
  it('only transcription and re-diarization are destructive', () => {
    expect(isDestructiveReprocess(['transcription'])).toBe(true);
    expect(isDestructiveReprocess(['rediarize', 'summarization'])).toBe(true);
    expect(isDestructiveReprocess(['summarization'])).toBe(false);
    expect(isDestructiveReprocess(undefined)).toBe(false);
  });

  it('the summary spinner starts only for a summarization stage with an LLM available', () => {
    expect(reprocessRegeneratesSummary(['summarization'], true)).toBe(true);
    expect(reprocessRegeneratesSummary(['summarization'], false)).toBe(false);
    expect(reprocessRegeneratesSummary(['transcription'], true)).toBe(false);
    expect(reprocessRegeneratesSummary(undefined, true)).toBe(false);
  });
});
