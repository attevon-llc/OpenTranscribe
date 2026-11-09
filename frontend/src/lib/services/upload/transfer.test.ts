import { describe, it, expect, vi } from 'vitest';

vi.mock('$stores/capabilities', () => ({
  capabilities: {
    subscribe: (run: (v: unknown) => void) => {
      run({ apiMediatedUploadEnabled: false });
      return () => {};
    },
  },
}));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (v: (k: string) => string) => void) => {
      run((k: string) => k);
      return () => {};
    },
  },
}));

import {
  apiMediatedUploadAllowed,
  assertLegacyFallbackAllowed,
  duplicateFromConflict,
  isNonRetryableCompleteFailure,
  transcriptionHeaders,
} from './transfer';
import type { UploadItem } from './types';

describe('transfer helpers', () => {
  it('reads a duplicate only from a 409 carrying the uuid', () => {
    const conflict = (data: unknown, status = 409) => ({ response: { status, data } });
    expect(duplicateFromConflict(conflict({ detail: { duplicate_file_uuid: 'u1' } }))).toEqual({
      uuid: 'u1',
      isDuplicate: true,
    });
    expect(duplicateFromConflict(conflict({ detail: {} }))).toBeNull();
    expect(
      duplicateFromConflict(conflict({ detail: { duplicate_file_uuid: 'u1' } }, 500))
    ).toBeNull();
    expect(duplicateFromConflict(new Error('net'))).toBeNull();
  });

  it('treats only 503 from /complete as non-retryable', () => {
    expect(isNonRetryableCompleteFailure({ response: { status: 503 } })).toBe(true);
    expect(isNonRetryableCompleteFailure({ response: { status: 500 } })).toBe(false);
    expect(isNonRetryableCompleteFailure(new Error('x'))).toBe(false);
  });

  it('builds legacy headers, keeping a speaker count of 0', () => {
    const u = {
      minSpeakers: 0,
      maxSpeakers: null,
      numSpeakers: 3,
      skipSummary: true,
    } as UploadItem;
    expect(transcriptionHeaders(u)).toEqual({
      'X-Min-Speakers': '0',
      'X-Num-Speakers': '3',
      'X-Skip-Summary': 'true',
    });
  });

  it('refuses the legacy fallback when the server disables it', () => {
    expect(apiMediatedUploadAllowed()).toBe(false);
    expect(() => assertLegacyFallbackAllowed()).toThrow(/unavailable/);
  });
});
