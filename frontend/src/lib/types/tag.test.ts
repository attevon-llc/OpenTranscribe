import { describe, it, expect } from 'vitest';
import { canMutateTag } from './tag';
import type { Tag, TagOwnership } from './tag';

const base: Tag = { uuid: 'u', name: 'n' };

describe('canMutateTag', () => {
  it.each<[TagOwnership | undefined, boolean, boolean]>([
    ['mine', false, true],
    ['mine', true, true],
    ['system', false, false],
    ['system', true, true],
    ['shared_with_me', false, false],
    ['shared_with_me', true, false],
    [undefined, false, true],
    [undefined, true, true],
  ])('ownership %s, admin %s -> %s', (ownership, isAdmin, expected) => {
    expect(canMutateTag({ ...base, ownership }, isAdmin)).toBe(expected);
  });
});
