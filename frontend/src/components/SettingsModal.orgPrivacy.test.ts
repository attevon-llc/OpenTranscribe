/**
 * Organization privacy navigation (issue #1280). The row is offered only to an organization
 * administrator, on a managed edition, with `org.defaults` granted to the org_admin audience.
 * The capabilities store is fail-open (an unknown key reads as enabled), so the audience check
 * is what keeps a missing grant from advertising the row; these pin each condition on its own.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

const edition = vi.hoisted(() => ({ cloud: true }));
vi.mock('$lib/edition', () => ({
  get isCloudEdition() {
    return edition.cloud;
  },
}));

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return {
    user: writable<{ role: string; org_role?: string } | null>(null),
    readAccountLifecycle: () => null,
  };
});
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));

const mockUserSettingsApi = vi.hoisted(() => ({
  getRecordingSettings: vi.fn(),
  updateRecordingSettings: vi.fn(),
  resetRecordingSettings: vi.fn(),
}));
vi.mock('$lib/api/userSettings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/userSettings')>();
  return { ...actual, UserSettingsApi: mockUserSettingsApi };
});
const policyApi = vi.hoisted(() => ({ getPolicy: vi.fn() }));
vi.mock('$lib/api/orgAdmin', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/orgAdmin')>();
  return { ...actual, OrgAdminApi: { ...actual.OrgAdminApi, getPolicy: policyApi.getPolicy } };
});
vi.mock('$lib/api/userApprovals', () => ({
  UserApprovalsApi: { list: vi.fn().mockResolvedValue([]) },
  isAlreadyDecided: () => false,
}));

import SettingsModal from './SettingsModal.svelte';
import { user as mockUser } from '$stores/auth';
import { settingsModalStore } from '$stores/settingsModalStore';
import { capabilities } from '$stores/capabilities';
import { resetAppStores } from '../test-mocks/app-stores';
import { sectionLoaders } from '$lib/settings/sectionLoaders';

const NAV = 'privacy.org.navLabel';

function setUser(role: string, orgRole?: string) {
  (mockUser as unknown as { set: (v: unknown) => void }).set({ role, org_role: orgRole });
}

function grant(caps: Record<string, boolean>, audience: Record<string, 'user' | 'org_admin'>) {
  capabilities.set({
    edition: 'cloud',
    loaded: true,
    capabilities: caps,
    audience,
    maxUploadBytes: undefined,
    tenancyMode: 'multi',
  });
}

beforeAll(async () => {
  await Promise.all(Object.values(sectionLoaders).map((load) => load()));
}, 180_000);

beforeEach(() => {
  vi.clearAllMocks();
  resetAppStores();
  settingsModalStore.reset();
  edition.cloud = true;
  mockAxios.get.mockResolvedValue({ data: {} });
  mockUserSettingsApi.getRecordingSettings.mockResolvedValue({
    max_recording_duration: 120,
    recording_quality: 'high',
    auto_stop_enabled: true,
  });
  policyApi.getPolicy.mockRejectedValue({ response: { status: 404 } });
});

async function open(section: 'recording' | 'org-privacy' = 'recording') {
  settingsModalStore.open(section);
  const view = render(SettingsModal);
  await waitFor(() =>
    expect(view.container.querySelector('.settings-sidebar .nav-item')).not.toBeNull()
  );
  return view.container;
}

function navLabels(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('.settings-sidebar .nav-item-label')).map(
    (el) => el.textContent?.trim() ?? ''
  );
}

const GRANTED = [{ 'org.defaults': true }, { 'org.defaults': 'org_admin' as const }] as const;

describe('organization privacy nav row', () => {
  it('is offered to an organization administrator when the capability is granted to that audience', async () => {
    setUser('user', 'org:admin');
    grant(GRANTED[0], GRANTED[1]);
    expect(navLabels(await open())).toContain(NAV);
  });

  it('is hidden for an organization member', async () => {
    setUser('user', 'org:member');
    grant(GRANTED[0], GRANTED[1]);
    expect(navLabels(await open())).not.toContain(NAV);
  });

  it('is hidden for a person with no organization role', async () => {
    setUser('admin');
    grant(GRANTED[0], GRANTED[1]);
    expect(navLabels(await open())).not.toContain(NAV);
  });

  it('is hidden when the capability is switched off', async () => {
    setUser('user', 'org:admin');
    grant({ 'org.defaults': false }, { 'org.defaults': 'org_admin' });
    expect(navLabels(await open())).not.toContain(NAV);
  });

  it('is hidden when the capability is absent (the store reads that as enabled, the audience does not)', async () => {
    setUser('user', 'org:admin');
    grant({}, {});
    expect(navLabels(await open())).not.toContain(NAV);
  });

  it('is hidden when the capability is aimed at ordinary users, not administrators', async () => {
    setUser('user', 'org:admin');
    grant({ 'org.defaults': true }, { 'org.defaults': 'user' });
    expect(navLabels(await open())).not.toContain(NAV);
  });

  it('is hidden in the community edition even with everything else granted', async () => {
    edition.cloud = false;
    setUser('user', 'org:admin');
    grant(GRANTED[0], GRANTED[1]);
    expect(navLabels(await open())).not.toContain(NAV);
  });
});

describe('organization privacy panel routing', () => {
  it('renders the panel for an organization administrator', async () => {
    setUser('user', 'org:admin');
    grant(GRANTED[0], GRANTED[1]);
    const container = await open('org-privacy');
    await waitFor(() => expect(policyApi.getPolicy).toHaveBeenCalled());
    expect(container.querySelector('.settings-content .section-title')?.textContent).toBe(
      'privacy.org.title'
    );
    expect(container.querySelector('[data-testid="org-privacy-panel"]')).not.toBeNull();
  });

  it('does not render the panel or call the API for a member who deep-links to it', async () => {
    setUser('user', 'org:member');
    grant(GRANTED[0], GRANTED[1]);
    const container = await open('org-privacy');
    // Give a (wrongly) rendered lazy panel time to mount before asserting its absence.
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(container.querySelector('[data-testid="org-privacy-panel"]')).toBeNull();
    expect(container.querySelector('.settings-content .section-title')?.textContent).not.toBe(
      'privacy.org.title'
    );
    expect(policyApi.getPolicy).not.toHaveBeenCalled();
  });
});
