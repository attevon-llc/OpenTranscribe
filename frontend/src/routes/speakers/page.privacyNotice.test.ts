/**
 * `/speakers` explains the privacy policy above its tabs (issue #1280). The page must mount
 * `SpeakerPrivacyNotice` for the signed-in user, and what it says follows the tab: clusters and
 * the inbox are voice-derived (both statements), the profiles tab only shows gender (so only the
 * gender statement). Everything else this route composes is stubbed; this file is about that
 * one wiring.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string) => en[key] ?? key;
  return { t: readable(t), locale: readable('en') };
});

const stub = vi.hoisted(() => async () => ({
  default: (await import('../../test-mocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/ConfirmationModal.svelte', stub);
vi.mock('$components/ui/BaseModal.svelte', stub);
vi.mock('$components/speakers/ClustersTab.svelte', stub);
vi.mock('$components/speakers/ProfilesTab.svelte', stub);
vi.mock('$components/speakers/InboxTab.svelte', stub);
vi.mock('$components/speakers/SpeakerPreviewPlayer.svelte', stub);
vi.mock('$components/RetrievalQualityNotice.svelte', stub);

const clusterApi = vi.hoisted(() => ({
  listClusters: vi.fn(),
  getUnverifiedSpeakers: vi.fn(),
  listProfiles: vi.fn(),
}));
vi.mock('$lib/api/speakerClusters', () => ({
  ...clusterApi,
  getClusterDetail: vi.fn(),
  updateCluster: vi.fn(),
  deleteCluster: vi.fn(),
  promoteCluster: vi.fn(),
  mergeClusters: vi.fn(),
  splitCluster: vi.fn(),
  triggerRecluster: vi.fn(),
  batchVerifySpeakers: vi.fn(),
  getSpeakerMediaPreview: vi.fn(),
  updateProfile: vi.fn(),
  deleteProfile: vi.fn(),
  uploadProfileAvatar: vi.fn(),
  deleteProfileAvatar: vi.fn(),
  confirmProfileGender: vi.fn(),
  unassignSpeakers: vi.fn(),
  RENAME_PROPAGATION_TIMEOUT_MS: 1000,
}));

const privacyApi = vi.hoisted(() => ({ getEffective: vi.fn() }));
vi.mock('$lib/api/privacy', () => ({ PrivacyApi: privacyApi }));

import Page from './+page.svelte';
import { setPage } from '../../test-mocks/app-stores';
import { apiCache } from '$lib/apiCache';
import { loadEffectivePrivacy, resetEffectivePrivacy } from '$stores/privacyPolicy';

const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;

const ON = { enabled: true, locked_by: null };
const OFF = { enabled: false, locked_by: 'organization' };
const effective = (voice: typeof ON | typeof OFF, gender: typeof ON | typeof OFF) => ({
  organization_uuid: null,
  voiceprint_matching: voice,
  gender_inference: gender,
});

async function settled() {
  await loadEffectivePrivacy();
  await tick();
}

beforeEach(() => {
  vi.clearAllMocks();
  resetEffectivePrivacy();
  apiCache.clear();
  localStorage.clear();
  clusterApi.listClusters.mockResolvedValue({ items: [], total: 0, pages: 0 });
  clusterApi.getUnverifiedSpeakers.mockResolvedValue({ items: [], total: 0, pages: 0 });
  clusterApi.listProfiles.mockResolvedValue([]);
});

describe('speakers page privacy notice', () => {
  it('says speaker recognition is off, for the signed-in user (no file scope)', async () => {
    setPage('/speakers');
    privacyApi.getEffective.mockResolvedValue(effective(OFF, ON));
    render(Page);
    expect(await screen.findByTestId('voiceprints-off-notice')).toBeInTheDocument();
    expect(privacyApi.getEffective).toHaveBeenCalledWith(undefined);
  });

  it('says nothing when voice matching is allowed and gender inference is off', async () => {
    setPage('/speakers');
    privacyApi.getEffective.mockResolvedValue(effective(ON, OFF));
    render(Page);
    await settled();
    expect(screen.queryByTestId('speaker-privacy-notice')).toBeNull();
  });

  it('adds the gender statement on the clusters tab while inference is on', async () => {
    setPage('/speakers');
    privacyApi.getEffective.mockResolvedValue(effective(ON, ON));
    render(Page);
    expect(await screen.findByTestId('gender-inferred-notice')).toBeInTheDocument();
  });

  it('keeps only the gender statement on the profiles tab (profiles show no voice-derived data)', async () => {
    setPage('/speakers?tab=profiles');
    privacyApi.getEffective.mockResolvedValue(effective(OFF, ON));
    render(Page);
    expect(await screen.findByTestId('gender-inferred-notice')).toBeInTheDocument();
    expect(screen.queryByTestId('voiceprints-off-notice')).toBeNull();
  });

  it('follows the tab when the person switches tabs', async () => {
    setPage('/speakers');
    privacyApi.getEffective.mockResolvedValue(effective(OFF, ON));
    render(Page);
    await screen.findByTestId('voiceprints-off-notice');

    await fireEvent.click(
      screen.getByRole('button', { name: new RegExp(en['speakers.tabs.profiles']) })
    );
    await settled();
    expect(screen.queryByTestId('voiceprints-off-notice')).toBeNull();
    expect(screen.getByTestId('gender-inferred-notice')).toBeInTheDocument();

    await fireEvent.click(screen.getByTestId('speakers-tab-inbox'));
    await settled();
    expect(screen.getByTestId('voiceprints-off-notice')).toBeInTheDocument();
  });
});
