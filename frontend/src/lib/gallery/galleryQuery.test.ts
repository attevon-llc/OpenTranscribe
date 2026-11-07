/** The gallery's query string, including the computed "Legal hold" filter (`held=true`). */
import { describe, it, expect } from 'vitest';
import { buildGalleryQuery, type GalleryQueryState } from './galleryQuery';

const base: GalleryQueryState = {
  page: 2,
  pageSize: 100,
  sortBy: 'upload_time',
  sortOrder: 'desc',
  searchQuery: '',
  selectedTags: [],
  selectedSpeakers: [],
  dateRange: { from: null, to: null },
  durationRange: { min: null, max: null },
  selectedFileTypes: [],
  selectedStatuses: [],
  ownershipFilter: 'all',
  selectedOwners: [],
  hiddenMode: 'exclude',
  held: false,
};

describe('buildGalleryQuery', () => {
  it('sends held=true only while the Legal hold filter is on', () => {
    expect(buildGalleryQuery(base).has('held')).toBe(false);
    expect(buildGalleryQuery({ ...base, held: true }).get('held')).toBe('true');
  });

  it('keeps every other filter the page used to send, in the same shape', () => {
    const query = buildGalleryQuery({
      ...base,
      searchQuery: 'board',
      selectedTags: ['a', 'b'],
      selectedSpeakers: ['S1'],
      dateRange: { from: new Date('2026-01-01T00:00:00Z'), to: null },
      durationRange: { min: 60, max: null },
      selectedFileTypes: ['audio'],
      selectedStatuses: ['completed'],
      ownershipFilter: 'mine',
      selectedOwners: ['o1'],
      hiddenMode: 'only',
    });
    expect(query.toString()).toBe(
      'page=2&page_size=100&sort_by=upload_time&sort_order=desc&search=board&tag=a&tag=b' +
        '&speaker=S1&from_date=2026-01-01T00%3A00%3A00.000Z&min_duration=60&file_type=audio' +
        '&status=completed&ownership=mine&owner=o1&hidden=only'
    );
  });
});
