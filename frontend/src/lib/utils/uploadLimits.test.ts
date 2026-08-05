import { describe, expect, it } from 'vitest';
import {
  LARGE_UPLOAD_WARNING_BYTES,
  MAX_UPLOAD_BYTES,
  exceedsUploadLimit,
  warrantsLargeUploadWarning,
} from './uploadLimits';

const GB = 1024 * 1024 * 1024;

describe('uploadLimits', () => {
  it('pins the hard limit to the backend max_filesize (15 GB)', () => {
    expect(MAX_UPLOAD_BYTES).toBe(15 * GB);
  });

  it('pins the soft warning threshold to 2 GB', () => {
    expect(LARGE_UPLOAD_WARNING_BYTES).toBe(2 * GB);
  });

  it('keeps the warning threshold below the hard limit', () => {
    expect(LARGE_UPLOAD_WARNING_BYTES).toBeLessThan(MAX_UPLOAD_BYTES);
  });

  describe('exceedsUploadLimit', () => {
    it('accepts files at or under 15 GB', () => {
      expect(exceedsUploadLimit(0)).toBe(false);
      expect(exceedsUploadLimit(500 * 1024 * 1024)).toBe(false);
      expect(exceedsUploadLimit(MAX_UPLOAD_BYTES)).toBe(false);
    });

    it('rejects files over 15 GB', () => {
      expect(exceedsUploadLimit(MAX_UPLOAD_BYTES + 1)).toBe(true);
      expect(exceedsUploadLimit(20 * GB)).toBe(true);
    });

    it('accepts the 2-15 GB band that the multi-file path used to reject (#298)', () => {
      // The exact regression: this file uploaded alone but was rejected in a batch.
      expect(exceedsUploadLimit(5 * GB)).toBe(false);
      expect(exceedsUploadLimit(2 * GB + 1)).toBe(false);
      expect(exceedsUploadLimit(14 * GB)).toBe(false);
    });
  });

  describe('warrantsLargeUploadWarning', () => {
    it('stays quiet at or under 2 GB', () => {
      expect(warrantsLargeUploadWarning(0)).toBe(false);
      expect(warrantsLargeUploadWarning(LARGE_UPLOAD_WARNING_BYTES)).toBe(false);
    });

    it('warns above 2 GB', () => {
      expect(warrantsLargeUploadWarning(LARGE_UPLOAD_WARNING_BYTES + 1)).toBe(true);
      expect(warrantsLargeUploadWarning(5 * GB)).toBe(true);
    });

    it('warns without rejecting across the whole 2-15 GB band', () => {
      for (const size of [3 * GB, 8 * GB, 15 * GB]) {
        expect(warrantsLargeUploadWarning(size)).toBe(true);
        expect(exceedsUploadLimit(size)).toBe(false);
      }
    });
  });
});
