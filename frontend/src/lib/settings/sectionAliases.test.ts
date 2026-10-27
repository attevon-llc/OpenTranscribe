import { describe, it, expect } from 'vitest';
import {
  SECTION_ALIASES,
  aliasIdsOf,
  dataTabFor,
  sidebarRowFor,
  speakerIdTabFor,
  statusTabFor,
  transcriptionTabFor,
  watchSourcesTabFor,
} from './sectionAliases';

describe('SECTION_ALIASES', () => {
  it.each([
    ['asr-provider', 'transcription', 'tx-provider'],
    ['custom-vocabulary', 'transcription', 'tx-vocabulary'],
    ['engine-settings', 'speaker-identification', 'spk-engine'],
    ['speaker-attributes', 'speaker-identification', 'spk-attributes'],
  ] as const)('%s opens %s > %s', (id, row, tab) => {
    expect(SECTION_ALIASES[id]).toEqual({ row, tab });
    expect(sidebarRowFor(id)).toBe(row);
    expect((row === 'transcription' ? transcriptionTabFor : speakerIdTabFor)(id)).toBe(tab);
    expect((row === 'transcription' ? speakerIdTabFor : transcriptionTabFor)(id)).toBeNull();
  });
});

describe('Data & Storage aliases', () => {
  it.each([
    ['retention', 'data-retention'],
    ['backup', 'data-backup'],
    ['data-integrity', 'data-integrity'],
    ['search-indexing', 'data-search'],
    ['embedding-migration', 'data-embeddings'],
  ] as const)('%s opens data-management > %s', (id, tab) => {
    expect(SECTION_ALIASES[id]).toEqual({ row: 'data-management', tab });
    expect(sidebarRowFor(id)).toBe('data-management');
    expect(dataTabFor(id)).toBe(tab);
    expect(transcriptionTabFor(id)).toBeNull();
    expect(speakerIdTabFor(id)).toBeNull();
  });

  it('lists exactly those ids for the row, and none for a row without aliases', () => {
    expect(aliasIdsOf('data-management').sort()).toEqual(
      ['backup', 'data-integrity', 'embedding-migration', 'retention', 'search-indexing'].sort()
    );
    expect(aliasIdsOf('profile')).toEqual([]);
  });

  it('is null for the row id itself, so the shell default tab applies', () => {
    expect(dataTabFor('data-management')).toBeNull();
    expect(sidebarRowFor('data-management')).toBe('data-management');
  });
});

describe('File status alias', () => {
  it('admin-task-health opens the Recovery tab of the file-status row', () => {
    expect(SECTION_ALIASES['admin-task-health']).toEqual({
      row: 'file-status',
      tab: 'status-recovery',
    });
    expect(sidebarRowFor('admin-task-health')).toBe('file-status');
    expect(statusTabFor('admin-task-health')).toBe('status-recovery');
    expect(dataTabFor('admin-task-health')).toBeNull();
    expect(aliasIdsOf('file-status')).toEqual(['admin-task-health']);
  });

  it('is null for the row id itself, so the Status tab applies', () => {
    expect(statusTabFor('file-status')).toBeNull();
    expect(sidebarRowFor('file-status')).toBe('file-status');
  });
});

describe('sidebarRowFor', () => {
  it('folds the redaction and chat admin aliases into their rows', () => {
    expect(sidebarRowFor('redaction-policy')).toBe('content-redaction');
    expect(sidebarRowFor('chat-admin')).toBe('chat');
  });

  it('leaves ordinary ids alone, including the two new rows', () => {
    expect(sidebarRowFor('profile')).toBe('profile');
    expect(sidebarRowFor('transcription')).toBe('transcription');
    expect(sidebarRowFor('speaker-identification')).toBe('speaker-identification');
    expect(sidebarRowFor('auto-labeling')).toBe('auto-labeling');
  });
});

describe('transcriptionTabFor / speakerIdTabFor', () => {
  it('are null for a row id, so the shell default applies', () => {
    for (const fn of [transcriptionTabFor, speakerIdTabFor]) {
      expect(fn('transcription')).toBeNull();
      expect(fn('speaker-identification')).toBeNull();
    }
  });

  it('are null for a row-only alias and for ordinary ids', () => {
    for (const fn of [transcriptionTabFor, speakerIdTabFor]) {
      expect(fn('redaction-policy')).toBeNull();
      expect(fn('profile')).toBeNull();
    }
  });
});

describe('Watch Sources aliases', () => {
  it.each([
    ['watch-sources-email', 'email'],
    ['watch-sources-global', 'global'],
  ] as const)('%s opens the %s tab of the watch-sources row', (id, tab) => {
    expect(SECTION_ALIASES[id]).toEqual({ row: 'watch-sources', tab });
    expect(sidebarRowFor(id)).toBe('watch-sources');
    expect(watchSourcesTabFor(id)).toBe(tab);
    expect(dataTabFor(id)).toBeNull();
    expect(transcriptionTabFor(id)).toBeNull();
  });

  it('is null for the row id itself, so the Sources tab default applies', () => {
    expect(watchSourcesTabFor('watch-sources')).toBeNull();
    expect(sidebarRowFor('watch-sources')).toBe('watch-sources');
  });
});
