import { describe, expect, it, beforeEach } from 'vitest';
import {
  DEFAULT_MAX_UPLOAD_BYTES,
  LARGE_UPLOAD_WARNING_BYTES,
  exceedsUploadLimit,
  getMaxUploadBytes,
  warrantsLargeUploadWarning,
} from './uploadLimits';
import { capabilities } from '$stores/capabilities';

const GB = 1024 * 1024 * 1024;

beforeEach(() => {
  capabilities.set({
    edition: 'community',
    loaded: false,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
  });
});

describe('uploadLimits', () => {
  it('pins the coded fallback default to 15 GB (matches the backend default)', () => {
    expect(DEFAULT_MAX_UPLOAD_BYTES).toBe(15 * GB);
  });

  it('pins the soft warning threshold to 2 GB', () => {
    expect(LARGE_UPLOAD_WARNING_BYTES).toBe(2 * GB);
  });

  it('keeps the warning threshold below the default hard limit', () => {
    expect(LARGE_UPLOAD_WARNING_BYTES).toBeLessThan(DEFAULT_MAX_UPLOAD_BYTES);
  });

  describe('getMaxUploadBytes — live value from $stores/capabilities', () => {
    it('falls back to the coded default before the capabilities fetch resolves', () => {
      // capabilities.maxUploadBytes is `undefined` pre-fetch (see beforeEach) —
      // this is the exact case that used to silently allow an unbounded upload
      // if it were misread as "no limit" instead of "unknown yet".
      expect(getMaxUploadBytes()).toBe(DEFAULT_MAX_UPLOAD_BYTES);
    });

    it('uses the fetched value once loaded, even when it differs from the default', () => {
      capabilities.set({
        edition: 'community',
        loaded: true,
        capabilities: {},
        audience: {},
        maxUploadBytes: 5 * GB,
      });

      expect(getMaxUploadBytes()).toBe(5 * GB);
    });

    it('treats an explicit null (admin disabled the limit) as "no limit", not "unknown"', () => {
      capabilities.set({
        edition: 'community',
        loaded: true,
        capabilities: {},
        audience: {},
        maxUploadBytes: null,
      });

      expect(getMaxUploadBytes()).toBeNull();
    });
  });

  describe('exceedsUploadLimit', () => {
    it('accepts files at or under the coded default when nothing has loaded yet', () => {
      expect(exceedsUploadLimit(0)).toBe(false);
      expect(exceedsUploadLimit(500 * 1024 * 1024)).toBe(false);
      expect(exceedsUploadLimit(DEFAULT_MAX_UPLOAD_BYTES)).toBe(false);
    });

    it('rejects files over the coded default when nothing has loaded yet', () => {
      expect(exceedsUploadLimit(DEFAULT_MAX_UPLOAD_BYTES + 1)).toBe(true);
      expect(exceedsUploadLimit(20 * GB)).toBe(true);
    });

    it('honors a fetched limit lower than the coded default (5 GB), not the hardcoded 15 GB', () => {
      capabilities.set({
        edition: 'community',
        loaded: true,
        capabilities: {},
        audience: {},
        maxUploadBytes: 5 * GB,
      });

      // A file the old hardcoded 15 GB literal would have accepted is now
      // correctly rejected against the admin's actual configured 5 GB ceiling.
      expect(exceedsUploadLimit(6 * GB)).toBe(true);
      expect(exceedsUploadLimit(5 * GB)).toBe(false);
    });

    it('honors a fetched limit higher than the coded default (30 GB)', () => {
      capabilities.set({
        edition: 'community',
        loaded: true,
        capabilities: {},
        audience: {},
        maxUploadBytes: 30 * GB,
      });

      // A file the old hardcoded 15 GB literal would have rejected is now
      // correctly accepted against the admin's actual configured 30 GB ceiling.
      expect(exceedsUploadLimit(20 * GB)).toBe(false);
      expect(exceedsUploadLimit(31 * GB)).toBe(true);
    });

    it('never rejects anything once the admin has disabled the limit', () => {
      capabilities.set({
        edition: 'community',
        loaded: true,
        capabilities: {},
        audience: {},
        maxUploadBytes: null,
      });

      expect(exceedsUploadLimit(500 * GB)).toBe(false);
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
