import { describe, it, expect } from 'vitest';
import {
  resolveSpeakerIdTab,
  speakerIdentificationTabs,
  speakerIdentificationVisible,
  type SpeakerIdAccess,
} from './speakerIdentificationTabs';

const access = (over: Partial<SpeakerIdAccess> = {}): SpeakerIdAccess => ({
  isAdmin: false,
  isSuperAdmin: false,
  prefsCap: true,
  engineCap: true,
  migrationCap: true,
  ...over,
});

const admin = { isAdmin: true };
const superAdmin = { isAdmin: true, isSuperAdmin: true };

describe('speakerIdentificationTabs', () => {
  it('gives a plain user detection and attributes only', () => {
    expect(speakerIdentificationTabs(access())).toEqual([
      { id: 'spk-detection', locked: false },
      { id: 'spk-attributes', locked: false },
    ]);
  });

  it('shows an admin the engine and maintenance tabs locked, not omitted', () => {
    expect(speakerIdentificationTabs(access(admin))).toEqual([
      { id: 'spk-detection', locked: false },
      { id: 'spk-attributes', locked: false },
      { id: 'spk-engine', locked: true },
      { id: 'spk-maintenance', locked: true },
    ]);
  });

  it('opens all four tabs for a super admin', () => {
    const tabs = speakerIdentificationTabs(access(superAdmin));
    expect(tabs.map((tab) => tab.id)).toEqual([
      'spk-detection',
      'spk-attributes',
      'spk-engine',
      'spk-maintenance',
    ]);
    expect(tabs.every((tab) => !tab.locked)).toBe(true);
  });

  it('drops the engine tab without the engine capability', () => {
    const ids = speakerIdentificationTabs(access({ ...superAdmin, engineCap: false })).map(
      (tab) => tab.id
    );
    expect(ids).not.toContain('spk-engine');
    expect(ids).toContain('spk-maintenance');
  });

  it('drops the maintenance tab without the migration capability', () => {
    const ids = speakerIdentificationTabs(access({ ...superAdmin, migrationCap: false })).map(
      (tab) => tab.id
    );
    expect(ids).not.toContain('spk-maintenance');
    expect(ids).toContain('spk-engine');
  });

  it('drops detection without the prefs capability but keeps attributes', () => {
    expect(speakerIdentificationTabs(access({ prefsCap: false })).map((tab) => tab.id)).toEqual([
      'spk-attributes',
    ]);
    expect(speakerIdentificationVisible(access({ prefsCap: false }))).toBe(true);
  });
});

describe('resolveSpeakerIdTab', () => {
  const adminTabs = speakerIdentificationTabs(access(admin));
  const superTabs = speakerIdentificationTabs(access(superAdmin));

  it('does not honour a locked tab: an admin asking for the engine lands on detection', () => {
    expect(resolveSpeakerIdTab('spk-engine', adminTabs)).toBe('spk-detection');
  });

  it('honours a requested tab that is openable', () => {
    expect(resolveSpeakerIdTab('spk-engine', superTabs)).toBe('spk-engine');
  });

  it('falls back to the first tab when everything is locked', () => {
    const locked = [
      { id: 'spk-engine' as const, locked: true },
      { id: 'spk-maintenance' as const, locked: true },
    ];
    expect(resolveSpeakerIdTab('spk-maintenance', locked)).toBe('spk-engine');
  });

  it('returns null with no tabs', () => {
    expect(resolveSpeakerIdTab('spk-detection', [])).toBeNull();
  });
});
