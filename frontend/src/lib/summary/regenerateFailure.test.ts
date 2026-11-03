import { describe, it, expect } from 'vitest';
import { restoreAfterFailedRegenerate, saveSummaryState } from './regenerateFailure';

describe('restoreAfterFailedRegenerate', () => {
  it('puts the summary flags back after they were cleared for the generating state', () => {
    const file = { uuid: 'f', has_summary: true, summary_opensearch_id: 'doc-1' };
    const saved = saveSummaryState(file);
    const cleared = { ...file, has_summary: false, summary_opensearch_id: null };

    const restored = restoreAfterFailedRegenerate(cleared, saved);

    expect(restored).toEqual(file);
    expect(restored).not.toBe(cleared);
  });

  it('leaves every other field of the file alone', () => {
    const saved = saveSummaryState({ has_summary: true, summary_opensearch_id: 'x' });
    const restored = restoreAfterFailedRegenerate(
      { uuid: 'f', filename: 'a.mp3', has_summary: false, summary_opensearch_id: null },
      saved
    );
    expect(restored.filename).toBe('a.mp3');
    expect(restored.uuid).toBe('f');
  });
});
