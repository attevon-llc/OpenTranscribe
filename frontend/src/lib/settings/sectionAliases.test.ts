import { describe, it, expect } from 'vitest';
import { SECTION_ALIASES, initialTabFor, sidebarRowFor } from './sectionAliases';

describe('SECTION_ALIASES', () => {
  it.each([
    ['asr-provider', 'transcription', 'tx-provider'],
    ['custom-vocabulary', 'transcription', 'tx-vocabulary'],
    ['engine-settings', 'speaker-identification', 'spk-engine'],
    ['speaker-attributes', 'speaker-identification', 'spk-attributes'],
  ] as const)('%s opens %s > %s', (id, row, tab) => {
    expect(SECTION_ALIASES[id]).toEqual({ row, tab });
    expect(sidebarRowFor(id)).toBe(row);
    expect(initialTabFor(id)).toBe(tab);
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

describe('initialTabFor', () => {
  it('is null for a row id, so the shell default applies', () => {
    expect(initialTabFor('transcription')).toBeNull();
    expect(initialTabFor('speaker-identification')).toBeNull();
  });

  it('is null for a row-only alias and for ordinary ids', () => {
    expect(initialTabFor('redaction-policy')).toBeNull();
    expect(initialTabFor('profile')).toBeNull();
  });
});
