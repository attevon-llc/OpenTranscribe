import { describe, it, expect, beforeEach } from 'vitest';
import {
  avatarFileError,
  clampPageAfterRemoval,
  loadCollapseState,
  resolveInboxShortcut,
  saveCollapseState,
  toggleMember,
} from './pageHelpers';

describe('clampPageAfterRemoval', () => {
  it('pulls the page back when the last item of the last page goes', () => {
    expect(clampPageAfterRemoval(2, 21, 1)).toBe(1);
  });
  it('leaves the page alone while it is still in range', () => {
    expect(clampPageAfterRemoval(1, 41, 1)).toBe(1);
  });
  it('never goes below page 1 when the list empties', () => {
    expect(clampPageAfterRemoval(3, 1, 1)).toBe(1);
  });
});

describe('toggleMember', () => {
  it('adds, removes, and does not mutate the input', () => {
    const base = new Set(['a']);
    const added = toggleMember(base, 'b');
    expect([...added].sort()).toEqual(['a', 'b']);
    expect([...base]).toEqual(['a']);
    expect([...toggleMember(added, 'a')]).toEqual(['b']);
  });
});

describe('collapse state', () => {
  beforeEach(() => localStorage.clear());
  it('defaults to open', () => {
    expect(loadCollapseState()).toEqual({ identified: false, unidentified: false });
  });
  it('round-trips and tolerates a partial or corrupt record', () => {
    saveCollapseState({ identified: true, unidentified: false });
    expect(loadCollapseState()).toEqual({ identified: true, unidentified: false });
    localStorage.setItem('speakers-cluster-sections-collapse', '{"unidentified":true}');
    expect(loadCollapseState()).toEqual({ identified: false, unidentified: true });
    localStorage.setItem('speakers-cluster-sections-collapse', '{not json');
    expect(loadCollapseState()).toEqual({ identified: false, unidentified: false });
  });
});

describe('avatarFileError', () => {
  it('rejects non-images and oversize files, accepts the 2 MB boundary', () => {
    expect(avatarFileError({ type: 'application/pdf', size: 1 })).toBe(
      'speakers.avatar.invalidType'
    );
    expect(avatarFileError({ type: 'image/png', size: 2 * 1024 * 1024 + 1 })).toBe(
      'speakers.avatar.tooLarge'
    );
    expect(avatarFileError({ type: 'image/png', size: 2 * 1024 * 1024 })).toBeNull();
  });
});

describe('resolveInboxShortcut', () => {
  const named = { suggested_name: 'Ann' };
  it('accepts only when the first item has a suggestion', () => {
    expect(resolveInboxShortcut('a', named)).toBe('accept');
    expect(resolveInboxShortcut('A', { suggested_name: null })).toBeNull();
  });
  it('maps s/n to skip and p to preview, needing an item', () => {
    expect(resolveInboxShortcut('S', named)).toBe('skip');
    expect(resolveInboxShortcut('n', named)).toBe('skip');
    expect(resolveInboxShortcut('p', named)).toBe('preview');
    expect(resolveInboxShortcut('p', undefined)).toBeNull();
    expect(resolveInboxShortcut('x', named)).toBeNull();
  });
});
