import { describe, it, expect } from 'vitest';
import {
  SECTION_ALIASES,
  aliasIdsOf,
  dataTabFor,
  sidebarRowFor,
  speakerIdTabFor,
  transcriptionTabFor,
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
