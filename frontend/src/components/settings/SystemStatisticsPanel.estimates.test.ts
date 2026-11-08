/**
 * On large tables the stats API returns planner estimates for the segment and speaker totals
 * (`total_segments_is_estimate`, `total_is_estimate`) instead of counting 10 million rows per
 * call. The panels must mark those totals as approximate rather than present them as exact.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';

const read = (file: string) => readFileSync(resolve(__dirname, file), 'utf8').split('<style>')[0];

describe('approximate totals in the statistics panels', () => {
  it('marks the segment and speaker totals as approximate when the API says so', () => {
    const panel = read('SystemStatisticsPanel.svelte');
    expect(panel).toMatch(/stats\.transcripts\?\.total_segments_is_estimate \? '≈' : ''/);
    expect(panel).toMatch(/stats\.speakers\?\.total_is_estimate \? '≈' : ''/);
  });

  it('marks the speaker total in the processing details the same way', () => {
    expect(read('ProcessingDetailsModal.svelte')).toMatch(
      /stats\.speakers\?\.total_is_estimate \? '≈' : ''/
    );
  });
});
