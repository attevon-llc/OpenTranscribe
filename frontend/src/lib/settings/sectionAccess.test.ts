import { describe, it, expect } from 'vitest';
import { SECTION_MIN_ROLE, lockReason, sectionLocked } from './sectionAccess';

const t = (key: string) => `T:${key}`;

describe('sectionLocked', () => {
  it('locks super_admin sections for plain admins and admin sections for users', () => {
    expect(sectionLocked('authentication', true, false)).toBe(true);
    expect(sectionLocked('authentication', true, true)).toBe(false);
    expect(sectionLocked('quarantine', false, false)).toBe(true);
    expect(sectionLocked('quarantine', true, false)).toBe(false);
  });

  it('never locks a section that is absent from the map', () => {
    expect(SECTION_MIN_ROLE.profile).toBeUndefined();
    expect(sectionLocked('profile', false, false)).toBe(false);
  });
});

describe('lockReason', () => {
  it('words the tooltip for the tier the section actually needs', () => {
    expect(lockReason('quarantine', t)).toBe('T:settings.permission.adminTitle');
    expect(lockReason('authentication', t)).toBe('T:settings.nav.requiresSuperAdmin');
  });
});
