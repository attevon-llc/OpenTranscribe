import { describe, it, expect } from 'vitest';
import { createFuzzyIndex, normalizeText } from './fuzzyMatcher';

interface Item {
  label: string;
  description?: string;
  keywords?: string[];
}

const items: Item[] = [
  { label: 'Transcription', description: 'source language and speaker counts' },
  { label: 'Content Redaction', description: 'mask PII and profanity' },
  { label: 'Backup', description: 'scheduled database backups', keywords: ['speaker export'] },
  { label: 'Speaker Attributes', description: 'per-speaker configuration' },
  { label: 'Vídeo Ajustes', description: 'opciones de vídeo' },
];

function makeIndex() {
  return createFuzzyIndex(items, {
    keys: [
      { name: 'label', weight: 3 },
      { name: 'description', weight: 1 },
      { name: 'keywords', weight: 1 },
    ],
  });
}

describe('normalizeText', () => {
  it('case-folds and strips diacritics', () => {
    expect(normalizeText('Vídeo')).toBe('video');
    expect(normalizeText('ÀÉÎÕÜ')).toBe('aeiou');
  });
});

describe('createFuzzyIndex', () => {
  it('returns nothing for an empty/whitespace query', () => {
    expect(makeIndex().search('')).toEqual([]);
    expect(makeIndex().search('   ')).toEqual([]);
  });

  it('ranks an exact label match first', () => {
    const results = makeIndex().search('transcription');
    expect(results[0].item.label).toBe('Transcription');
  });

  it('tolerates typos', () => {
    const results = makeIndex().search('transcibe');
    expect(results.map((r) => r.item.label)).toContain('Transcription');
  });

  it('weights label matches above description matches', () => {
    const results = makeIndex().search('speaker');
    const labels = results.map((r) => r.item.label);
    // "Speaker Attributes" (label hit) should outrank items that only mention
    // "speaker" in description/keywords.
    expect(labels[0]).toBe('Speaker Attributes');
  });

  it('folds diacritics so an unaccented query matches accented text', () => {
    const results = makeIndex().search('video');
    expect(results.map((r) => r.item.label)).toContain('Vídeo Ajustes');
  });

  it('honors an explicit result limit', () => {
    const results = makeIndex().search('a', 2); // broad query
    expect(results.length).toBeLessThanOrEqual(2);
  });

  it('refreshes its collection via setCollection', () => {
    const index = createFuzzyIndex<Item>([], { keys: ['label'] });
    expect(index.search('backup')).toEqual([]);
    index.setCollection(items);
    expect(index.search('backup').map((r) => r.item.label)).toContain('Backup');
  });
});
