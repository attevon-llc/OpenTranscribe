/**
 * DEFECT THIS CATCHES: the card's bar going backwards (95% -> 93%, 90% -> 81% in a live run)
 * when the plan or a failed task changed the set of weighted tasks mid-run.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import type { ProcessingJobModel } from './processingJob';
import { resetProgressFloor, withMonotonicProgress } from './progressFloor';

const card = (percent: number, over: Partial<ProcessingJobModel> = {}) =>
  ({ key: 'job:f1', percent, run: 1000, ...over }) as ProcessingJobModel;

beforeEach(resetProgressFloor);

describe('withMonotonicProgress', () => {
  it('never lowers the percent within a run', () => {
    expect(withMonotonicProgress(card(90)).percent).toBe(90);
    expect(withMonotonicProgress(card(81)).percent).toBe(90);
    expect(withMonotonicProgress(card(95)).percent).toBe(95);
    expect(withMonotonicProgress(card(93)).percent).toBe(95);
  });

  it('starts over on a new run (a reprocess restarts the transcription)', () => {
    withMonotonicProgress(card(100));
    expect(withMonotonicProgress(card(5, { run: 2000 })).percent).toBe(5);
    expect(withMonotonicProgress(card(3, { run: 2000 })).percent).toBe(5);
  });

  it('tracks each card on its own', () => {
    withMonotonicProgress(card(80, { key: 'job:a' }));
    expect(withMonotonicProgress(card(10, { key: 'job:b' })).percent).toBe(10);
  });
});
