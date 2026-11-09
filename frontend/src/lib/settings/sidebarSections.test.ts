import { describe, it, expect, vi } from 'vitest';

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

import type { CapabilitiesState } from '$stores/capabilities';
import { buildSectionAccess } from './sectionCapabilities';
import { buildSidebarSections, capOn, type SidebarContext } from './sidebarSections';

const caps = (disabled: string[] = []): CapabilitiesState => ({
  edition: 'community',
  loaded: true,
  capabilities: Object.fromEntries(disabled.map((k) => [k, false])),
  audience: {},
  maxUploadBytes: undefined,
  apiMediatedUploadEnabled: true,
});

function ctx(
  over: Partial<SidebarContext> & {
    admin?: boolean;
    superAdmin?: boolean;
    disabled?: string[];
  } = {}
): SidebarContext {
  const capState = caps(over.disabled);
  const isAdmin = over.admin ?? false;
  const isSuperAdmin = over.superAdmin ?? false;
  const access = buildSectionAccess(capState, isAdmin, isSuperAdmin);
  return {
    t: (k) => k,
    capState,
    isAdmin,
    isSuperAdmin,
    orgRole: undefined,
    tenancyMulti: false,
    supportRequestCount: 0,
    pendingApprovalCount: 0,
    hasTakedowns: false,
    legalAvailable: false,
    transcriptionAccess: access.transcription,
    speakerIdAccess: access.speakerId,
    dataAccess: access.data,
    privacyAccess: access.privacy,
    ...over,
  };
}

const ids = (c: SidebarContext) => buildSidebarSections(c).flatMap((s) => s.items.map((i) => i.id));
const titles = (c: SidebarContext) => buildSidebarSections(c).map((s) => s.title);

describe('buildSidebarSections', () => {
  it('shows a plain user the account/AI/media/system groups, in the documented order', () => {
    const c = ctx();
    expect(titles(c)).toEqual([
      'settings.sections.account',
      'settings.sections.transcription',
      'settings.sections.aiChat',
      'settings.sections.mediaOutput',
      'settings.sections.system',
    ]);
    expect(ids(c).slice(0, 3)).toEqual(['profile', 'groups', 'processing-pipeline']);
  });

  it('adds the administration group only for admins, with locked rows greyed not dropped', () => {
    expect(titles(ctx())).not.toContain('settings.sections.administration');
    const admin = buildSidebarSections(ctx({ admin: true }));
    const group = admin.find((s) => s.title === 'settings.sections.administration');
    const auth = group?.items.find((i) => i.id === 'authentication');
    expect(auth?.locked).toBe(true);
    expect(group?.items.find((i) => i.id === 'quarantine')?.locked).toBe(false);
    const superAdmin = buildSidebarSections(ctx({ admin: true, superAdmin: true }));
    expect(superAdmin.flatMap((s) => s.items).find((i) => i.id === 'authentication')?.locked).toBe(
      false
    );
  });

  it('drops a row whose capability the deployment lacks, and an emptied group with it', () => {
    expect(ids(ctx({ disabled: ['sharing.teams'] }))).not.toContain('groups');
    const noMedia = ctx({
      disabled: ['url_ingest', 'media_sources', 'watch_sources', 'recording', 'audio_extraction'],
    });
    expect(titles(noMedia)).not.toContain('settings.sections.mediaOutput');
  });

  it('carries the badge counts and normalises a missing badge to 0', () => {
    const rows = buildSidebarSections(
      ctx({ admin: true, pendingApprovalCount: 4, tenancyMulti: true, supportRequestCount: 2 })
    ).flatMap((s) => s.items);
    expect(rows.find((i) => i.id === 'admin-users')?.badge).toBe(4);
    expect(rows.find((i) => i.id === 'support-access-requests')?.badge).toBe(2);
    expect(rows.find((i) => i.id === 'profile')?.badge).toBe(0);
  });

  it('shows conditional rows only when their flag is on', () => {
    expect(ids(ctx())).not.toContain('legal');
    expect(ids(ctx({ legalAvailable: true }))).toContain('legal');
    expect(ids(ctx())).not.toContain('my-takedowns');
    expect(ids(ctx({ hasTakedowns: true }))).toContain('my-takedowns');
  });

  it('never offers the cloud billing group on the community edition', () => {
    expect(titles(ctx({ admin: true, superAdmin: true }))).not.toContain(
      'settings.sections.billingTeam'
    );
  });
});

describe('capOn', () => {
  it('treats no key as ungated and an explicit false as off', () => {
    expect(capOn(caps(['x']), undefined)).toBe(true);
    expect(capOn(caps(['x']), 'x')).toBe(false);
    expect(capOn(caps(), 'unknown')).toBe(true);
  });
});
