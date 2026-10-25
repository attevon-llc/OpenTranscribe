/**
 * localStorage persistence for the DOCX/PDF export options (line numbering, page size and the
 * same timestamps / speakers toggles as the TXT export). Preference storage only: nothing here
 * touches transcript content or redaction policy. A stored value outside the allowed set is
 * dropped, so a stale or tampered blob can never put an unknown value on the wire.
 */

export type LineNumbering = 'per_page' | 'continuous' | 'none';
export type PageSize = 'a4' | 'letter';

export interface DocumentPrefs {
  includeTimestamps: boolean;
  includeSpeakers: boolean;
  lineNumbering: LineNumbering;
  pageSize: PageSize;
}

const KEY = 'opentranscribe.documentExportPrefs';
const LINE_NUMBERINGS: readonly string[] = ['per_page', 'continuous', 'none'];
const PAGE_SIZES: readonly string[] = ['a4', 'letter'];

const DEFAULTS: DocumentPrefs = {
  includeTimestamps: true,
  includeSpeakers: true,
  lineNumbering: 'per_page',
  pageSize: 'a4',
};

export function loadDocumentPrefs(): DocumentPrefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULTS };
    const stored = JSON.parse(raw) as Partial<Record<keyof DocumentPrefs, unknown>>;
    return {
      includeTimestamps:
        typeof stored.includeTimestamps === 'boolean'
          ? stored.includeTimestamps
          : DEFAULTS.includeTimestamps,
      includeSpeakers:
        typeof stored.includeSpeakers === 'boolean'
          ? stored.includeSpeakers
          : DEFAULTS.includeSpeakers,
      lineNumbering: LINE_NUMBERINGS.includes(String(stored.lineNumbering))
        ? (stored.lineNumbering as LineNumbering)
        : DEFAULTS.lineNumbering,
      pageSize: PAGE_SIZES.includes(String(stored.pageSize))
        ? (stored.pageSize as PageSize)
        : DEFAULTS.pageSize,
    };
  } catch {
    return { ...DEFAULTS };
  }
}

export function saveDocumentPrefs(prefs: DocumentPrefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    // Storage may be full or disabled; the options simply do not persist.
  }
}
