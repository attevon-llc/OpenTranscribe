/**
 * `SettingsModal.svelte` is the one place a section's required privilege tier
 * (`SECTION_MIN_ROLE`) turns into what actually renders: the sidebar, the mobile
 * picker, and the content router all key off the same `sectionLocked()` call, and
 * per `components/settings/CLAUDE.md` a section the user lacks privilege for must
 * render disabled-with-a-tooltip, never be omitted (an admin once concluded a page
 * did not exist because it was silently dropped). That gating, the unsaved-changes
 * close confirmation, and the mount-time badge/data orchestration are exactly the
 * "complex derived state and multi-step orchestration" this suite scopes to — the
 * ~40 child settings panels themselves are out of scope here (each is/should be
 * tested independently, and Playwright already exercises this modal end to end).
 */
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { user: writable<{ role: string } | null>(null), readAccountLifecycle: () => null };
});

vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
  locale: {
    subscribe: (run: (value: string) => void) => {
      run('en');
      return () => {};
    },
  },
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

const mockUserApprovalsApi = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock('$lib/api/userApprovals', () => ({
  UserApprovalsApi: mockUserApprovalsApi,
  isAlreadyDecided: () => false,
}));

// The redaction panels' own loading is out of scope here; a request that never settles
// keeps them in their loading state so only the tab shell is under test.
vi.mock('$lib/api/redactionSettings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/redactionSettings')>();
  const pending = () => new Promise(() => {});
  return {
    ...actual,
    getRedactionSettings: pending,
    getRedactionDefaults: pending,
    getRedactionPolicy: pending,
  };
});

import SettingsModal from './SettingsModal.svelte';
import { user as mockUser } from '$stores/auth';
import { settingsModalStore, type SettingsSection } from '$stores/settingsModalStore';
import { capabilities } from '$stores/capabilities';
import { resetAppStores } from '../test-mocks/app-stores';
import { sectionLoaders } from '$lib/settings/sectionLoaders';
import { fullLocalView } from '$lib/settings/pipelineViewFixtures';

function setUser(role: 'user' | 'admin' | 'super_admin' | null) {
  (mockUser as unknown as { set: (value: { role: string } | null) => void }).set(
    role ? { role } : null
  );
}

// Sections load lazily, so vitest would otherwise transform each panel's module tree inside the
// test that first opens it. Do that once up front (it used to happen while collecting the static
// imports), so test timeouts measure the component, not the transformer.
beforeAll(async () => {
  await Promise.all(Object.values(sectionLoaders).map((load) => load()));
}, 180_000);

beforeEach(() => {
  vi.clearAllMocks();
  resetAppStores();
  settingsModalStore.reset();
  capabilities.set({
    edition: 'community',
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
  });
  mockAxios.get.mockResolvedValue({ data: {} });
  mockAxios.post.mockResolvedValue({ data: {} });
  mockUserSettingsApi.getRecordingSettings.mockResolvedValue({
    max_recording_duration: 120,
    recording_quality: 'high',
    auto_stop_enabled: true,
  });
  mockUserApprovalsApi.list.mockResolvedValue([]);
  setUser('user');
});

/** Opens on 'recording' — an inline form section with no heavy child component,
 * so the sidebar's own gating/derived-state logic is under test in isolation. */
function openModal() {
  settingsModalStore.open('recording');
  return render(SettingsModal);
}

/** The nav label and the open panel's own heading share the same i18n key, so
 * a plain findByText is ambiguous — wait on the content pane's heading instead. */
async function waitForOpen(container: HTMLElement) {
  await waitFor(() =>
    expect(container.querySelector('.settings-content .section-title')).not.toBeNull()
  );
  // Section panels load lazily: the heading is there at once, the panel a moment later.
  // Generous: under vitest each lazily imported panel is transformed on demand, not prebuilt.
  await waitFor(
    () => expect(container.querySelector('[data-testid="settings-section-loading"]')).toBeNull(),
    { timeout: 10_000 }
  );
}

function navItem(container: HTMLElement, label: string): HTMLElement | null {
  return (
    (Array.from(container.querySelectorAll('.settings-sidebar .nav-item')).find(
      (el) => el.querySelector('.nav-item-label')?.textContent?.includes(label)
    ) as HTMLElement | undefined) ?? null
  );
}

describe('privilege-gated sidebar sections', () => {
  it('omits admin-only groups entirely for a plain user', async () => {
    setUser('user');
    const { container } = openModal();
    await waitForOpen(container);

    expect(navItem(container, 'settings.users.title')).toBeNull();
    expect(navItem(container, 'settings.authentication.title')).toBeNull();
  });

  it('renders a super-admin-only section disabled with a lock, not omitted, for a plain admin', async () => {
    setUser('admin');
    const { container } = openModal();
    await waitForOpen(container);

    const authItem = navItem(container, 'settings.authentication.title');
    expect(authItem).not.toBeNull();
    expect(authItem).toHaveAttribute('disabled');
    expect(authItem?.classList.contains('locked')).toBe(true);
    expect(authItem?.getAttribute('title')).toBe('settings.nav.requiresSuperAdmin');
    expect(authItem?.querySelector('.lock-indicator')).not.toBeNull();
  });

  it('unlocks the same section for a super admin', async () => {
    setUser('super_admin');
    const { container } = openModal();
    await waitForOpen(container);

    const authItem = navItem(container, 'settings.authentication.title');
    expect(authItem).not.toHaveAttribute('disabled');
    expect(authItem?.classList.contains('locked')).toBe(false);
  });

  it('drops a section entirely when its capability is disabled by the backend', async () => {
    capabilities.set({
      edition: 'community',
      loaded: true,
      capabilities: { 'asr.user_providers': false, 'prompts.user': false },
      audience: {},
      maxUploadBytes: undefined,
    });
    setUser('user');
    const { container } = openModal();
    await waitForOpen(container);

    // Absent entirely — distinct from a privilege-gated item, which stays
    // present, disabled, and carrying a lock icon (checked in the test above).
    expect(navItem(container, 'settings.aiPrompts.title')).toBeNull();
  });

  // Issue #1141: the URL Import Quality entry was gated on `exports`, so it showed
  // whenever exports were on even with URL import off (and vanished with exports off
  // while URL import was on). FileUploader gates URL import on `url_ingest`.
  describe('URL Import Quality tab', () => {
    function withCaps(caps: Record<string, boolean>) {
      capabilities.set({
        edition: 'community',
        loaded: true,
        capabilities: caps,
        audience: {},
        maxUploadBytes: undefined,
      });
    }

    it('is absent when url_ingest is disabled, even with exports enabled', async () => {
      withCaps({ url_ingest: false, exports: true });
      const { container } = openModal();
      await waitForOpen(container);

      expect(navItem(container, 'settings.download.title')).toBeNull();
    });

    it('is present when url_ingest is enabled, even with exports disabled', async () => {
      withCaps({ url_ingest: true, exports: false });
      const { container } = openModal();
      await waitForOpen(container);

      expect(navItem(container, 'settings.download.title')).not.toBeNull();
    });
  });
});

describe('merged Privacy & Redaction section', () => {
  function sectionOf(container: HTMLElement, label: string): string | null {
    const item = navItem(container, label);
    return (
      item?.closest('.sidebar-section')?.querySelector('.section-heading')?.textContent?.trim() ??
      null
    );
  }

  it('lists ONE redaction row, not separate Content Redaction and Redaction Policy rows', async () => {
    setUser('super_admin');
    const { container } = openModal();
    await waitForOpen(container);

    expect(navItem(container, 'settings.privacyRedaction.title')).not.toBeNull();
    expect(navItem(container, 'settings.contentRedaction.title')).toBeNull();
    expect(navItem(container, 'settings.redactionPolicy.title')).toBeNull();
  });

  it('files the redaction row under AI & Chat right after Chat, not under Account', async () => {
    setUser('user');
    capabilities.set({
      edition: 'community',
      loaded: true,
      capabilities: { 'chat.rag': true },
      audience: {},
      maxUploadBytes: undefined,
    });
    const { container } = openModal();
    await waitForOpen(container);

    expect(sectionOf(container, 'settings.privacyRedaction.title')).toBe(
      'settings.sections.aiChat'
    );
    const labels = Array.from(
      navItem(container, 'settings.privacyRedaction.title')!
        .closest('.sidebar-section')!
        .querySelectorAll('.nav-item-label')
    ).map((el) => el.textContent?.trim());
    expect(labels.indexOf('settings.privacyRedaction.title')).toBe(
      labels.indexOf('chat.settings.title') + 1
    );
    const account = navItem(container, 'settings.profile.title')!.closest('.sidebar-section')!;
    expect(account.textContent).not.toContain('settings.privacyRedaction.title');
  });

  it('pins the Privacy & Redaction and Watch Sources tab strips', async () => {
    for (const section of ['content-redaction', 'watch-sources'] as const) {
      setUser('super_admin');
      settingsModalStore.open(section);
      const { container, unmount } = render(SettingsModal);
      await waitFor(
        () => expect(container.querySelector('.settings-content [role="tablist"]')).not.toBeNull(),
        { timeout: 10_000 }
      );
      expect(container.querySelector('.settings-content [role="tablist"]')).toHaveClass('sticky');
      unmount();
      settingsModalStore.reset();
    }
  }, 30_000);

  it('gives a plain user the redaction row but no tab strip (a single panel)', async () => {
    setUser('user');
    settingsModalStore.open('content-redaction');
    const { container } = render(SettingsModal);
    await waitForOpen(container);

    expect(navItem(container, 'settings.privacyRedaction.title')).not.toBeNull();
    expect(container.querySelector('[data-testid="privacy-redaction-panel"]')).not.toBeNull();
    expect(container.querySelector('[role="tab"]')).toBeNull();
  });

  it('shows an admin the policy tab disabled with the super-admin reason', async () => {
    setUser('admin');
    settingsModalStore.open('content-redaction');
    const { container } = render(SettingsModal);
    await waitForOpen(container);

    const policyTab = container.querySelector('#tab-policy') as HTMLButtonElement | null;
    expect(policyTab).not.toBeNull();
    expect(policyTab?.disabled).toBe(true);
    expect(policyTab?.getAttribute('title')).toBe('settings.nav.requiresSuperAdmin');
    expect(container.querySelector('#tab-personal')?.getAttribute('aria-selected')).toBe('true');
  });

  it('opens the policy tab for a super admin via the legacy redaction-policy id', async () => {
    setUser('super_admin');
    settingsModalStore.open('redaction-policy');
    const { container } = render(SettingsModal);
    await waitForOpen(container);

    expect(container.querySelector('#tab-policy')?.getAttribute('aria-selected')).toBe('true');
    // the alias highlights the merged row rather than leaving the sidebar blank
    expect(
      navItem(container, 'settings.privacyRedaction.title')?.classList.contains('active')
    ).toBe(true);
  });

  it('lands a plain admin deep-linking to redaction-policy on the personal tab, not a blank pane', async () => {
    setUser('admin');
    settingsModalStore.open('redaction-policy');
    const { container } = render(SettingsModal);
    await waitForOpen(container);

    expect(container.querySelector('#tab-personal')?.getAttribute('aria-selected')).toBe('true');
  });
});

describe('Transcription and Speaker Identification sections', () => {
  function sectionOf(container: HTMLElement, label: string): string | null {
    const item = navItem(container, label);
    return (
      item?.closest('.sidebar-section')?.querySelector('.section-heading')?.textContent?.trim() ??
      null
    );
  }

  const engineValue = (value: unknown) => ({ value, source: 'default' });
  async function openAs(role: 'user' | 'admin' | 'super_admin', section: SettingsSection) {
    mockAxios.get.mockImplementation(async (url: string) => ({
      data:
        url === '/admin/engine-settings'
          ? {
              diarizer_backend: engineValue('native'),
              diarizer_require_sidecar: engineValue(false),
              boundary_smoothing_enabled: engineValue(true),
              boundary_acoustic_recheck_enabled: engineValue(false),
              boundary_acoustic_cosine_margin: engineValue(0.05),
              boundary_acoustic_max_word_dur: engineValue(1.0),
            }
          : url === '/embeddings/migration/status'
            ? { current_mode: 'v4', migration_needed: false }
            : {},
    }));
    setUser(role);
    settingsModalStore.open(section);
    const view = render(SettingsModal);
    await waitForOpen(view.container);
    return view;
  }
  const tab = (container: HTMLElement, id: string) =>
    container.querySelector(`#tab-${id}`) as HTMLButtonElement | null;

  it('has no engine-settings, asr-provider, vocabulary or attributes row, for any role', async () => {
    for (const role of ['user', 'admin', 'super_admin'] as const) {
      const { container, unmount } = await openAs(role, 'recording');
      for (const key of [
        'settings.engineSettings.title',
        'settings.asrProvider.title',
        'settings.customVocabulary.title',
        'settings.speakerAttributes.navTitle',
      ]) {
        expect(navItem(container, key)).toBeNull();
      }
      unmount();
    }
  });

  it('lists the two rows under one group, and Auto-Labeling under AI & Chat', async () => {
    const { container } = await openAs('user', 'recording');
    expect(sectionOf(container, 'settings.transcription.title')).toBe(
      'settings.sections.transcription'
    );
    expect(sectionOf(container, 'settings.speakerIdentification.title')).toBe(
      'settings.sections.transcription'
    );
    expect(sectionOf(container, 'autoLabel.title')).toBe('settings.sections.aiChat');
  });

  it('shows a plain user the speaker-identification row but not the engine or maintenance tabs', async () => {
    const { container } = await openAs('user', 'speaker-identification');
    expect(
      container.querySelector('[data-testid="speaker-identification-section"]')
    ).not.toBeNull();
    expect(tab(container, 'spk-detection')).not.toBeNull();
    expect(tab(container, 'spk-attributes')).not.toBeNull();
    expect(tab(container, 'spk-engine')).toBeNull();
    expect(tab(container, 'spk-maintenance')).toBeNull();
  });

  it('opens engine-settings as an admin on the section, with the engine tab locked and not selected', async () => {
    const { container } = await openAs('admin', 'engine-settings');
    expect(
      navItem(container, 'settings.speakerIdentification.title')?.classList.contains('active')
    ).toBe(true);
    const engine = tab(container, 'spk-engine');
    expect(engine?.disabled).toBe(true);
    expect(engine?.getAttribute('aria-selected')).toBe('false');
    expect(engine?.getAttribute('title')).toBe('settings.nav.requiresSuperAdmin');
  });

  it.each([
    ['super_admin', 'engine-settings', 'spk-engine'],
    ['user', 'speaker-attributes', 'spk-attributes'],
    ['user', 'asr-provider', 'tx-provider'],
    ['user', 'custom-vocabulary', 'tx-vocabulary'],
    ['user', 'transcription', 'tx-language'],
    ['user', 'speaker-identification', 'spk-detection'],
  ] as const)('%s opening %s lands on tab %s', async (role, section, tabId) => {
    const { container } = await openAs(role, section as SettingsSection);
    expect(tab(container, tabId)?.getAttribute('aria-selected')).toBe('true');
  });

  it('keeps each old id working in the mobile picker with a matching option', async () => {
    const { container } = await openAs('user', 'asr-provider');
    const select = container.querySelector('.mobile-nav-select') as HTMLSelectElement;
    expect(select.value).toBe('transcription');
  });

  it('drops the provider tab, not the whole row, when the ASR capability is off', async () => {
    capabilities.set({
      edition: 'community',
      loaded: true,
      capabilities: { 'asr.user_providers': false },
      audience: {},
      maxUploadBytes: undefined,
    });
    const { container } = await openAs('user', 'transcription');
    expect(navItem(container, 'settings.transcription.title')).not.toBeNull();
    expect(tab(container, 'tx-provider')).toBeNull();
    expect(tab(container, 'tx-language')).not.toBeNull();
  });

  it('follows the Maintenance link card to the Speaker Embedding System section', async () => {
    const { container } = await openAs('super_admin', 'speaker-identification');
    await fireEvent.click(tab(container, 'spk-maintenance') as HTMLElement);
    const button = await waitFor(() => {
      const el = container.querySelector('[data-testid="embedding-link-card"] button');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    await fireEvent.click(button);

    await waitFor(() =>
      expect(
        navItem(container, 'settings.embeddingMigration.title')?.classList.contains('active')
      ).toBe(true)
    );
  });

  it('jumps from the Automatic detection line to Transcription > Provider & Model', async () => {
    const { container } = await openAs('admin', 'speaker-identification');
    const link = await waitFor(() => {
      const el = container.querySelector('[data-testid="detection-credential-line"] button');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    await fireEvent.click(link);

    await waitFor(() =>
      expect(navItem(container, 'settings.transcription.title')?.classList.contains('active')).toBe(
        true
      )
    );
    await waitFor(
      () => expect(tab(container, 'tx-provider')?.getAttribute('aria-selected')).toBe('true'),
      { timeout: 10_000 }
    );
  });

  describe('revealing the active sidebar row', () => {
    const scrolled: { row: Element | null; opts: ScrollIntoViewOptions | undefined }[] = [];

    beforeEach(() => {
      scrolled.length = 0;
      Element.prototype.scrollIntoView = function (
        this: Element,
        opts?: boolean | ScrollIntoViewOptions
      ) {
        scrolled.push({ row: this, opts: opts as ScrollIntoViewOptions | undefined });
      };
    });

    afterEach(() => {
      // jsdom ships no scrollIntoView, so removing the stub restores the original state.
      delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
      vi.unstubAllGlobals();
    });

    async function followMaintenanceLink(container: HTMLElement) {
      await fireEvent.click(tab(container, 'spk-maintenance') as HTMLElement);
      const button = await waitFor(() => {
        const el = container.querySelector('[data-testid="embedding-link-card"] button');
        expect(el).not.toBeNull();
        return el as HTMLElement;
      });
      await fireEvent.click(button);
    }

    it('scrolls the Speaker Embedding System row into view when the link card jumps there', async () => {
      const { container } = await openAs('super_admin', 'speaker-identification');
      scrolled.length = 0;
      await followMaintenanceLink(container);

      const embedding = navItem(container, 'settings.embeddingMigration.title');
      await waitFor(() => expect(scrolled.some((c) => c.row === embedding)).toBe(true));
      expect(embedding?.classList.contains('active')).toBe(true);
      const call = scrolled.find((c) => c.row === embedding);
      expect(call?.opts?.block).toBe('nearest');
    });

    it('does not animate the scroll when the user prefers reduced motion', async () => {
      vi.stubGlobal('matchMedia', (query: string) => ({
        matches: query.includes('prefers-reduced-motion'),
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      }));
      const { container } = await openAs('super_admin', 'speaker-identification');
      scrolled.length = 0;
      await followMaintenanceLink(container);

      const embedding = navItem(container, 'settings.embeddingMigration.title');
      await waitFor(() => expect(scrolled.some((c) => c.row === embedding)).toBe(true));
      expect(scrolled.find((c) => c.row === embedding)?.opts?.behavior).toBe('auto');
    });
  });

  it('locks the embedding migration controls for an admin without calling its routes (#1208)', async () => {
    const { container } = await openAs('admin', 'embedding-migration');
    await waitFor(() =>
      expect(container.querySelector('[data-testid="embedding-migration-locked"]')).not.toBeNull()
    );
    expect(mockAxios.get).not.toHaveBeenCalledWith('/embeddings/migration/status');
  });

  it('leaves the embedding migration unlocked and loaded for a super admin (#1208)', async () => {
    const { container } = await openAs('super_admin', 'embedding-migration');
    await waitFor(() => expect(mockAxios.get).toHaveBeenCalledWith('/embeddings/migration/status'));
    expect(container.querySelector('[data-testid="embedding-migration-locked"]')).toBeNull();
  });

  it('keeps the modal heading the first .section-title in the content area', async () => {
    const { container } = await openAs('super_admin', 'speaker-attributes');
    const first = container.querySelector('.settings-content .section-title');
    expect(first?.textContent?.trim()).toBe('settings.speakerIdentification.title');
  });

  it('passes the unsaved flag of a child form up to the section row', async () => {
    const { container } = await openAs('user', 'speaker-identification');
    await waitFor(() =>
      expect(container.querySelector('#diarization-source, #speaker-behavior')).not.toBeNull()
    );
    const select = container.querySelector('#speaker-behavior') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: 'use_custom' } });
    await waitFor(() =>
      expect(
        navItem(container, 'settings.speakerIdentification.title')?.classList.contains('dirty')
      ).toBe(true)
    );
  });
});

describe('settings search placement', () => {
  it('keeps the search outside the scrolling nav list so a scrollbar cannot overlap it', async () => {
    const { container } = openModal();
    await waitForOpen(container);

    const sidebar = container.querySelector('.settings-sidebar') as HTMLElement;
    const scroller = sidebar.querySelector('.sidebar-scroll') as HTMLElement;
    const search = sidebar.querySelector('#settings-search-desktop-input') as HTMLElement;

    expect(scroller).not.toBeNull();
    expect(scroller.contains(search)).toBe(false);
    expect(scroller.querySelectorAll('.nav-item').length).toBeGreaterThan(0);
    expect(sidebar.querySelectorAll('.nav-item').length).toBe(
      scroller.querySelectorAll('.nav-item').length
    );
    // Search first, list after: the search is a fixed header, not part of the scrolled content.
    expect(sidebar.firstElementChild?.contains(search)).toBe(true);
    expect(sidebar.firstElementChild).not.toBe(scroller);
  });

  it('raises the search header only once the nav list has scrolled', async () => {
    const { container } = openModal();
    await waitForOpen(container);
    const scroller = container.querySelector('.settings-sidebar .sidebar-scroll') as HTMLElement;
    const header = () =>
      container.querySelector('.settings-sidebar .settings-search') as HTMLElement;

    expect(header().classList.contains('raised')).toBe(false);
    scroller.scrollTop = 120;
    await fireEvent.scroll(scroller);
    expect(header().classList.contains('raised')).toBe(true);
    scroller.scrollTop = 0;
    await fireEvent.scroll(scroller);
    expect(header().classList.contains('raised')).toBe(false);
  });

  it('gives the mobile instance the same landmark and search input', async () => {
    const { container } = openModal();
    await waitForOpen(container);
    const mobile = container.querySelector('.mobile-section-nav') as HTMLElement;
    expect(mobile.querySelector('[role="search"]')).not.toBeNull();
    expect(mobile.querySelector('input[type="search"]')).not.toBeNull();
  });
});

describe('pending-approval badge', () => {
  it('fetches the count on mount for an admin and shows it on the Users nav item', async () => {
    mockUserApprovalsApi.list.mockResolvedValue([{ uuid: 'a' }, { uuid: 'b' }]);
    setUser('admin');
    const { container } = openModal();
    await waitForOpen(container);

    await waitFor(() => {
      const usersItem = navItem(container, 'settings.users.title');
      expect(usersItem?.querySelector('.nav-badge')?.textContent?.trim()).toBe('2');
    });
  });

  it('does not fetch the approval count for a non-admin', async () => {
    setUser('user');
    const { container } = openModal();
    await waitForOpen(container);

    expect(mockUserApprovalsApi.list).not.toHaveBeenCalled();
  });

  it('falls back to zero (no badge) when the count fetch fails, rather than a stuck phantom count', async () => {
    mockUserApprovalsApi.list.mockRejectedValue(new Error('boom'));
    setUser('admin');
    const { container } = openModal();
    await waitForOpen(container);

    await waitFor(() => expect(mockUserApprovalsApi.list).toHaveBeenCalled());
    expect(navItem(container, 'settings.users.title')?.querySelector('.nav-badge')).toBeNull();
  });
});

describe('unsaved-changes close confirmation', () => {
  it('closes immediately via Escape when nothing is dirty', async () => {
    const { container } = openModal();
    await waitForOpen(container);

    await fireEvent.keyDown(document, { key: 'Escape' });

    await waitFor(() => expect(container.querySelector('.settings-modal')).toBeNull());
  });

  it('shows a confirmation instead of closing when a section has unsaved changes, and force-closes on confirm', async () => {
    const { container, getByText } = openModal();
    await waitForOpen(container);

    settingsModalStore.setDirty('recording', true);
    const closeBtn = container.querySelector('.modal-close-button') as HTMLElement;
    await fireEvent.click(closeBtn);

    // The settings dialog is still open, plus a confirmation surfaced.
    expect(container.querySelector('.settings-modal')).not.toBeNull();
    const confirmBtn = getByText('settings.closeWithoutSaving') as HTMLElement;
    await fireEvent.click(confirmBtn);

    await waitFor(() => expect(container.querySelector('.settings-modal')).toBeNull());
  });

  it('cancelling the confirmation leaves the modal open and the section still dirty', async () => {
    const { container, getByText } = openModal();
    await waitForOpen(container);

    settingsModalStore.setDirty('recording', true);
    await fireEvent.click(container.querySelector('.modal-close-button') as HTMLElement);
    const cancelBtn = getByText('settings.keepEditing') as HTMLElement;
    await fireEvent.click(cancelBtn);

    expect(container.querySelector('.settings-modal')).not.toBeNull();
    let dirty = false;
    settingsModalStore.subscribe((s) => (dirty = s.dirtyState.recording))();
    expect(dirty).toBe(true);
  });
});

describe('section-switch data loading', () => {
  it('fetches system stats when the System Statistics section is opened', async () => {
    setUser('user');
    const { container } = openModal();
    await waitForOpen(container);
    mockAxios.get.mockClear();

    const statsItem = navItem(container, 'settings.statistics.title') as HTMLElement;
    await fireEvent.click(statsItem);

    await waitFor(() => expect(mockAxios.get).toHaveBeenCalledWith('/system/stats'));
  });
});

describe('Processing pipeline row (#1227)', () => {
  const rowsOfTranscriptionGroup = (container: HTMLElement) =>
    Array.from(container.querySelectorAll('.sidebar-section'))
      .find(
        (group) =>
          group.querySelector('.section-heading')?.textContent === 'settings.sections.transcription'
      )
      ?.querySelectorAll('.nav-item-label') ?? [];

  it('is the first row of the Transcription group, above Transcription itself', async () => {
    const { container } = openModal();
    await waitForOpen(container);

    const labels = Array.from(rowsOfTranscriptionGroup(container)).map(
      (el) => el.textContent?.trim()
    );
    expect(labels[0]).toBe('settings.pipeline.title');
    expect(labels).toContain('settings.transcription.title');
  });

  it('is absent when the deployment turns transcription preferences off', async () => {
    capabilities.set({
      edition: 'community',
      loaded: true,
      capabilities: { 'transcription.prefs': false },
      audience: {},
      maxUploadBytes: undefined,
    });
    const { container } = openModal();
    await waitForOpen(container);

    expect(navItem(container, 'settings.pipeline.title')).toBeNull();
  });

  it('opens the read-only view, which asks the server for the plan', async () => {
    const { container } = openModal();
    await waitForOpen(container);
    mockAxios.get.mockClear();
    mockAxios.get.mockImplementation(async (url: string) => ({
      data: url === '/user-settings/pipeline' ? fullLocalView() : {},
    }));

    await fireEvent.click(navItem(container, 'settings.pipeline.title') as HTMLElement);

    await waitFor(() => expect(mockAxios.get).toHaveBeenCalledWith('/user-settings/pipeline'));
    await waitFor(() =>
      expect(container.querySelectorAll('[data-testid="pipeline-step"]')).toHaveLength(4)
    );
  });
});

describe('opening the modal fetches each thing once', () => {
  // The modal is mounted already open (the layout imports it on demand), so BOTH the onMount
  // loaders and the "just opened" block ran and each endpoint was requested twice.
  const callsTo = (path: string) => mockAxios.get.mock.calls.filter(([url]) => url === path).length;

  it('requests the statistics bundle once when the statistics section is the landing section', async () => {
    setUser('admin');
    settingsModalStore.open('system-statistics');
    const { container } = render(SettingsModal);
    await waitForOpen(container);
    await waitFor(() => expect(callsTo('/system/stats')).toBeGreaterThan(0));

    expect(callsTo('/system/stats')).toBe(1);
    expect(callsTo('/search/reindex/status')).toBe(1);
    expect(callsTo('/search/index-health')).toBe(1);
  });

  it('requests the pending-approval count once for an admin', async () => {
    setUser('admin');
    settingsModalStore.open('system-statistics');
    const { container } = render(SettingsModal);
    await waitForOpen(container);

    expect(mockUserApprovalsApi.list).toHaveBeenCalledTimes(1);
  });

  it('a later section switch still refetches (single-flight is not a cache)', async () => {
    setUser('admin');
    const { container } = openModal();
    await waitForOpen(container);
    mockAxios.get.mockClear();

    await fireEvent.click(navItem(container, 'settings.statistics.title') as HTMLElement);
    await waitFor(() => expect(callsTo('/system/stats')).toBe(1));
  });
});
