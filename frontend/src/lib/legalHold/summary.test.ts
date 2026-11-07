import { describe, it, expect } from 'vitest';
import { markHeld } from './summary';

describe('markHeld', () => {
  it('marks a free file held by the authority that placed the hold', () => {
    expect(markHeld(undefined, ['platform'])).toEqual({
      held: true,
      held_by: ['platform'],
      hold_uuids: [],
    });
  });

  it('adds a second holder once, keeps the server hold uuids', () => {
    expect(
      markHeld({ held: true, held_by: ['platform'], hold_uuids: ['h1'] }, [
        'organization',
        'platform',
      ])
    ).toEqual({ held: true, held_by: ['organization', 'platform'], hold_uuids: ['h1'] });
  });
});
