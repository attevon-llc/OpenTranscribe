/**
 * The computed "Legal hold" collection in the gallery store (legal-hold plan, Lane D): it is a
 * collection of its own, so turning it on clears a selected collection; the page hears the
 * change (to reload) unless the caller reloads itself; a filter reset turns it off.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { galleryStore } from './gallery';

const blankFilters = {
  searchQuery: '',
  selectedTags: [],
  selectedSpeakers: [],
  selectedCollectionId: 'col-1',
  dateRange: { from: null, to: null },
  durationRange: { min: null, max: null },
  selectedFileTypes: [],
  selectedStatuses: [],
  ownershipFilter: 'all' as const,
  selectedOwners: [],
  sortBy: 'upload_time',
  sortOrder: 'desc' as const,
};

beforeEach(() => galleryStore.resetFilters());

describe('setHeldFilter', () => {
  it('turns the held filter on, clears a selected collection, and tells the page', () => {
    galleryStore.saveFilters(blankFilters);
    const heard: boolean[] = [];
    const stop = galleryStore.onHeldChanged((held) => heard.push(held));

    galleryStore.setHeldFilter(true);

    expect(get(galleryStore).filterHeld).toBe(true);
    expect(get(galleryStore).filterSelectedCollectionId).toBeNull();
    expect(heard).toEqual([true]);
    stop();
  });

  it('turns it off quietly when the caller reloads the list itself', () => {
    galleryStore.setHeldFilter(true);
    const heard: boolean[] = [];
    const stop = galleryStore.onHeldChanged((held) => heard.push(held));

    galleryStore.setHeldFilter(false, false);

    expect(get(galleryStore).filterHeld).toBe(false);
    expect(heard).toEqual([]);
    stop();
  });

  it('is turned off by a filter reset', () => {
    galleryStore.setHeldFilter(true);
    galleryStore.resetFilters();
    expect(get(galleryStore).filterHeld).toBe(false);
  });
});

describe('triggerPlaceHold', () => {
  it('reaches a subscriber, but not on subscribe', () => {
    let calls = 0;
    const stop = galleryStore.onPlaceHoldTrigger(() => (calls += 1));
    expect(calls).toBe(0);
    galleryStore.triggerPlaceHold();
    expect(calls).toBe(1);
    stop();
  });
});
