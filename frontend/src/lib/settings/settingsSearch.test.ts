import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const h = vi.hoisted(() => ({ initialized: true }));
vi.mock('i18next', () => ({
  default: {
    get isInitialized() {
      return h.initialized;
    },
    language: 'en',
    getResourceBundle: () => ({ 'settings.profile.title': 'Profile' }),
  },
}));

import type { CapabilitiesState } from '$stores/capabilities';
import { buildSectionAccess } from './sectionCapabilities';
import {
  buildSettingsFuzzyIndex,
  buildVisibleSections,
  flashSettingControl,
  type SearchAccess,
} from './settingsSearch';

const caps: CapabilitiesState = {
  edition: 'community',
  loaded: true,
  capabilities: {},
  audience: {},
  maxUploadBytes: undefined,
  apiMediatedUploadEnabled: true,
};
const t = (k: string) => `T:${k}`;

function access(isAdmin: boolean, isSuperAdmin: boolean): SearchAccess {
  const a = buildSectionAccess(caps, isAdmin, isSuperAdmin);
  return {
    isAdmin,
    isSuperAdmin,
    transcriptionAccess: a.transcription,
    speakerIdAccess: a.speakerId,
    dataAccess: a.data,
    statusAccess: a.status,
    privacyAccess: a.privacy,
  };
}
const row = (id: string, locked = false) => ({ id, label: id.toUpperCase(), locked });
const idsOf = (
  sidebar: { items: { id: string; label: string; locked: boolean }[] }[],
  a: SearchAccess
) => buildVisibleSections(t, sidebar, a).map((s) => s.id);

describe('buildVisibleSections', () => {
  it('lists unlocked sidebar rows and leaves locked ones out', () => {
    const ids = idsOf(
      [{ items: [row('profile'), row('authentication', true)] }],
      access(false, false)
    );
    expect(ids).toContain('profile');
    expect(ids).not.toContain('authentication');
  });

  it('adds the legacy alias ids whose tab the user can open', () => {
    const ids = idsOf([{ items: [row('profile')] }], access(true, true));
    expect(ids).toEqual(
      expect.arrayContaining([
        'asr-provider',
        'custom-vocabulary',
        'retention',
        'backup',
        'engine-settings',
      ])
    );
    const plain = idsOf([{ items: [row('profile')] }], access(false, false));
    expect(plain).not.toContain('retention');
    expect(plain).not.toContain('backup');
  });

  it('only exposes the admin-only tabs of a row where that row is open to the user', () => {
    const sidebar = [{ items: [row('watch-sources'), row('chat')] }];
    expect(idsOf(sidebar, access(true, true))).toEqual(
      expect.arrayContaining(['watch-sources-email', 'chat-admin'])
    );
    expect(idsOf(sidebar, access(true, false))).not.toContain('watch-sources-email');
    expect(idsOf(sidebar, access(false, false))).not.toContain('chat-admin');
    expect(
      idsOf([{ items: [row('chat', true), row('watch-sources', true)] }], access(true, true))
    ).not.toContain('chat-admin');
  });

  it('localises alias labels', () => {
    const label = buildVisibleSections(t, [{ items: [] }], access(true, true)).find(
      (s) => s.id === 'retention'
    )?.label;
    expect(label).toBe('T:settings.retention.title');
  });
});

describe('buildSettingsFuzzyIndex', () => {
  afterEach(() => {
    h.initialized = true;
  });

  it('is null until i18next is ready, and an index afterwards', () => {
    h.initialized = false;
    expect(buildSettingsFuzzyIndex([])).toBeNull();
    h.initialized = true;
    expect(buildSettingsFuzzyIndex([])).not.toBeNull();
  });
});

describe('flashSettingControl', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = '';
  });
  afterEach(() => vi.useRealTimers());

  it('schedules nothing without an anchor', () => {
    flashSettingControl(document.body, '');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('retries while the panel loads, then flashes the located control and clears the flash', () => {
    const modal = document.createElement('div');
    modal.innerHTML = '<div class="settings-content"></div>';
    document.body.append(modal);
    Element.prototype.scrollIntoView = vi.fn();
    flashSettingControl(modal, 'Max duration');
    vi.advanceTimersByTime(100);
    const content = modal.querySelector('.settings-content') as HTMLElement;
    content.innerHTML = '<div class="form-group"><label>Max duration</label><input /></div>';
    vi.advanceTimersByTime(100);
    const group = content.querySelector('.form-group') as HTMLElement;
    expect(group.classList.contains('settings-search-flash')).toBe(true);
    vi.advanceTimersByTime(1700);
    expect(group.classList.contains('settings-search-flash')).toBe(false);
  });

  it('gives up after about a second', () => {
    const modal = document.createElement('div');
    modal.innerHTML = '<div class="settings-content"></div>';
    const spy = vi.spyOn(window, 'setTimeout');
    flashSettingControl(modal, 'never there');
    vi.advanceTimersByTime(5000);
    expect(spy.mock.calls.filter((c) => c[1] === 50).length).toBe(20);
  });
});
