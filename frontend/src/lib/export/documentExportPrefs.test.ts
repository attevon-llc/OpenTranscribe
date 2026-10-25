import { describe, it, expect, beforeEach } from 'vitest';
import { loadDocumentPrefs, saveDocumentPrefs } from './documentExportPrefs';

const KEY = 'opentranscribe.documentExportPrefs';

beforeEach(() => localStorage.clear());

describe('document export preferences', () => {
  it('defaults to numbered-per-page A4 with timestamps and speakers', () => {
    expect(loadDocumentPrefs()).toEqual({
      includeTimestamps: true,
      includeSpeakers: true,
      lineNumbering: 'per_page',
      pageSize: 'a4',
    });
  });

  it('round-trips what was saved', () => {
    const prefs = {
      includeTimestamps: false,
      includeSpeakers: true,
      lineNumbering: 'continuous' as const,
      pageSize: 'letter' as const,
    };
    saveDocumentPrefs(prefs);
    expect(loadDocumentPrefs()).toEqual(prefs);
  });

  it('merges a partial blob over the defaults', () => {
    localStorage.setItem(KEY, JSON.stringify({ pageSize: 'letter' }));
    expect(loadDocumentPrefs()).toMatchObject({ pageSize: 'letter', lineNumbering: 'per_page' });
  });

  it('drops a value outside the allowed set instead of sending it to the server', () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ lineNumbering: 'sideways', pageSize: 'a0', includeTimestamps: 'yes' })
    );
    expect(loadDocumentPrefs()).toEqual({
      includeTimestamps: true,
      includeSpeakers: true,
      lineNumbering: 'per_page',
      pageSize: 'a4',
    });
  });

  it('survives a corrupt blob', () => {
    localStorage.setItem(KEY, '{not json');
    expect(loadDocumentPrefs().lineNumbering).toBe('per_page');
  });
});
