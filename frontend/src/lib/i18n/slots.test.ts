import { describe, it, expect } from 'vitest';
import { SLOT, splitAtSlot } from './slots';

describe('splitAtSlot', () => {
  it('splits a translated sentence around its single slot', () => {
    expect(splitAtSlot(`Licensed under the ${SLOT}.`)).toEqual(['Licensed under the ', '.']);
  });

  it('honours a slot that opens the sentence (word order differs by language)', () => {
    expect(splitAtSlot(`${SLOT} governs this software`)).toEqual(['', ' governs this software']);
  });

  it('keeps the whole sentence, then the link, when a translation lost its placeholder', () => {
    expect(splitAtSlot('No placeholder here')).toEqual(['No placeholder here ', '']);
  });

  it('splits only at the first slot', () => {
    expect(splitAtSlot(`a ${SLOT} b ${SLOT} c`)).toEqual(['a ', ` b ${SLOT} c`]);
  });

  it('uses a character no translation would type', () => {
    expect(SLOT.codePointAt(0)).toBeGreaterThanOrEqual(0xe000);
    expect(SLOT.codePointAt(0)).toBeLessThanOrEqual(0xf8ff);
  });
});
