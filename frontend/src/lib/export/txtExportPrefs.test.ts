/**
 * `txtExportPrefs.ts`'s own header locks the contract: defaults are both-on, and
 * a partial stored blob merges over the defaults rather than dropping a toggle.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { loadTxtPrefs, saveTxtPrefs } from './txtExportPrefs';

const KEY = 'opentranscribe.txtExportPrefs';

beforeEach(() => {
  localStorage.clear();
});

describe('loadTxtPrefs', () => {
  it('defaults both toggles on when nothing is stored', () => {
    expect(loadTxtPrefs()).toEqual({ includeTimestamps: true, includeSpeakers: true });
  });

  it('merges a partial stored blob over the defaults rather than dropping a toggle', () => {
    localStorage.setItem(KEY, JSON.stringify({ includeSpeakers: false }));
    expect(loadTxtPrefs()).toEqual({ includeTimestamps: true, includeSpeakers: false });
  });

  it('respects a fully-specified stored blob', () => {
    localStorage.setItem(KEY, JSON.stringify({ includeTimestamps: false, includeSpeakers: false }));
    expect(loadTxtPrefs()).toEqual({ includeTimestamps: false, includeSpeakers: false });
  });

  it('falls back to defaults instead of throwing on corrupt JSON', () => {
    localStorage.setItem(KEY, '{not json');
    expect(loadTxtPrefs()).toEqual({ includeTimestamps: true, includeSpeakers: true });
  });
});

describe('saveTxtPrefs', () => {
  it('persists prefs that loadTxtPrefs then reads back unchanged', () => {
    saveTxtPrefs({ includeTimestamps: false, includeSpeakers: true });
    expect(loadTxtPrefs()).toEqual({ includeTimestamps: false, includeSpeakers: true });
  });

  it('does not throw when localStorage.setItem fails (e.g. quota exceeded)', () => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = () => {
      throw new DOMException('QuotaExceededError');
    };
    try {
      saveTxtPrefs({ includeTimestamps: true, includeSpeakers: true });
      // The failed save must not have partially written — loadTxtPrefs still
      // reports the untouched defaults rather than a half-applied preference.
      expect(loadTxtPrefs()).toEqual({ includeTimestamps: true, includeSpeakers: true });
    } finally {
      Storage.prototype.setItem = original;
    }
  });
});
