/**
 * The Settings modal's sidebar: which rows exist for this user and deployment.
 *
 * Pure: everything it reads arrives in `SidebarContext`, so the modal keeps the reactive state and
 * this file keeps the (large, easily-regressed) row table. `SECTION_MIN_ROLE` in `sectionAccess.ts`
 * stays the single source of privilege truth.
 */
import type { CapabilitiesState } from '$stores/capabilities';
import { isCapabilityEnabled } from '$stores/capabilities';
import type { SettingsSection } from '$stores/settingsModalStore';
import { isCloudEdition } from '$lib/edition';
import { orgPrivacyVisible } from '$lib/settings/privacyPolicy';
import { orgAdminCapOn as orgCapOn } from '$lib/settings/orgAdminAccess';
import {
  privacyRedactionVisible,
  type PrivacyRedactionAccess,
} from '$lib/settings/privacyRedactionTabs';
import { transcriptionVisible, type TranscriptionAccess } from '$lib/settings/transcriptionTabs';
import {
  speakerIdentificationVisible,
  type SpeakerIdAccess,
} from '$lib/settings/speakerIdentificationTabs';
import { dataManagementVisible, type DataAccess } from '$lib/settings/dataManagementTabs';
import { sectionLocked } from '$lib/settings/sectionAccess';

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** A section-gating capability check: no key means ungated; otherwise the server-driven entitlement. */
export const capOn = (state: CapabilitiesState, key?: string): boolean =>
  !key || isCapabilityEnabled(state, key);

/** Cloud-edition org-admin gating, shared with the legal-hold actions. */
export const orgAdminCapOn = (state: CapabilitiesState, key: string): boolean =>
  orgCapOn(state, key, isCloudEdition);

/**
 * The privacy panel (issue #1280) additionally needs the person to BE an organization
 * administrator, and it lives in core: a deployment that replaces $lib/cloud still gets it.
 */
export const orgPrivacyOn = (state: CapabilitiesState, orgRole: string | undefined): boolean =>
  orgPrivacyVisible({
    isCloudEdition,
    capabilityEnabled: isCapabilityEnabled(state, 'org.defaults'),
    audience: state.audience['org.defaults'],
    orgRole,
  });

export interface SidebarContext {
  t: Translate;
  capState: CapabilitiesState;
  isAdmin: boolean;
  isSuperAdmin: boolean;
  orgRole: string | undefined;
  tenancyMulti: boolean;
  supportRequestCount: number;
  pendingApprovalCount: number;
  hasTakedowns: boolean;
  legalAvailable: boolean;
  transcriptionAccess: TranscriptionAccess;
  speakerIdAccess: SpeakerIdAccess;
  dataAccess: DataAccess;
  privacyAccess: PrivacyRedactionAccess;
}

interface RawItem {
  id: SettingsSection;
  label: string;
  icon: string;
  cap?: string;
  badge?: number;
}
export interface RawSection {
  title: string;
  items: RawItem[];
}

function accountGroup(ctx: SidebarContext): RawSection {
  const { t, tenancyMulti, supportRequestCount, hasTakedowns } = ctx;
  return {
    title: t('settings.sections.account'),
    items: [
      { id: 'profile' as SettingsSection, label: t('settings.profile.title'), icon: 'user' },
      {
        id: 'groups' as SettingsSection,
        label: t('groups.title'),
        icon: 'group',
        cap: 'sharing.teams',
      },
      ...(tenancyMulti
        ? [
            {
              id: 'support-access-requests' as SettingsSection,
              label: t('settings.supportAccessRequests.navLabel'),
              icon: 'life-buoy',
              badge: supportRequestCount,
            },
          ]
        : []),
      // issue #1254: only for a user who has had a file taken down; it is the durable copy of the notice.
      ...(hasTakedowns
        ? [
            {
              id: 'my-takedowns' as SettingsSection,
              label: t('takedown.mine.navLabel'),
              icon: 'shield-off',
            },
          ]
        : []),
    ],
  };
}

function transcriptionGroup(ctx: SidebarContext): RawSection {
  const { t, transcriptionAccess, speakerIdAccess } = ctx;
  return {
    title: t('settings.sections.transcription'),
    items: [
      // Two jobs, two rows, each with tabs: words (language, provider and model, vocabulary,
      // accuracy) and speakers (detection, attributes, engine, maintenance). The old rows
      // asr-provider / custom-vocabulary / engine-settings / speaker-attributes survive as
      // aliases that open their tab (see lib/settings/sectionAliases.ts).
      // Read-only overview first: where each step runs, with links to the rows below that edit it.
      ...(transcriptionAccess.prefsCap
        ? [
            {
              id: 'processing-pipeline' as SettingsSection,
              label: t('settings.pipeline.title'),
              icon: 'flow',
            },
          ]
        : []),
      ...(transcriptionVisible(transcriptionAccess)
        ? [
            {
              id: 'transcription' as SettingsSection,
              label: t('settings.transcription.title'),
              icon: 'waveform',
            },
          ]
        : []),
      ...(speakerIdentificationVisible(speakerIdAccess)
        ? [
            {
              id: 'speaker-identification' as SettingsSection,
              label: t('settings.speakerIdentification.title'),
              icon: 'user',
            },
          ]
        : []),
    ],
  };
}

function aiChatGroup(ctx: SidebarContext): RawSection {
  const { t, privacyAccess } = ctx;
  return {
    title: t('settings.sections.aiChat'),
    items: [
      // Admin platform tuning for chat lives in this panel's Advanced tab, not a second row.
      {
        id: 'chat' as SettingsSection,
        label: t('chat.settings.title'),
        icon: 'message',
        cap: 'chat.rag',
      },
      // Content analysis, not an account setting: ML detectors on the redaction worker, and the
      // policy decides what an AI model may see. One row, two tabs: the per-user preference and
      // (admin) the policy floor that overrides it.
      ...(privacyRedactionVisible(privacyAccess)
        ? [
            {
              id: 'content-redaction' as SettingsSection,
              label: t('settings.privacyRedaction.title'),
              icon: 'eye-off',
            },
          ]
        : []),
      {
        id: 'llm-provider' as SettingsSection,
        label: t('settings.llmProvider.title'),
        icon: 'brain',
        cap: 'llm.user_settings',
      },
      {
        id: 'llm-usage' as SettingsSection,
        label: t('settings.llmUsage.title'),
        icon: 'activity',
        cap: 'llm.user_settings',
      },
      {
        id: 'ai-prompts' as SettingsSection,
        label: t('settings.aiPrompts.title'),
        icon: 'message',
        cap: 'prompts.user',
      },
      // Org context is prompt material: OrganizationContextSettings persists
      // include_in_default_prompts / include_in_custom_prompts and nothing else reads it.
      {
        id: 'organization-context' as SettingsSection,
        label: isCloudEdition
          ? t('settings.orgContext.cloudTitle')
          : t('settings.orgContext.title'),
        icon: 'briefcase',
      },
      // Tags and collections, not speakers: it sat in the Transcription group only by history.
      { id: 'auto-labeling' as SettingsSection, label: t('autoLabel.title'), icon: 'tag' },
    ],
  };
}

function mediaGroup(ctx: SidebarContext): RawSection {
  const { t } = ctx;
  return {
    title: t('settings.sections.mediaOutput'),
    items: [
      {
        id: 'download' as SettingsSection,
        label: t('settings.download.title'),
        icon: 'download',
        cap: 'url_ingest',
      },
      {
        id: 'media-sources' as SettingsSection,
        label: t('settings.mediaSources.title'),
        icon: 'link',
        cap: 'media_sources',
      },
      {
        id: 'watch-sources' as SettingsSection,
        label: t('settings.watchSources.title'),
        icon: 'eye',
        cap: 'watch_sources',
      },
      {
        id: 'recording' as SettingsSection,
        label: t('settings.recording.title'),
        icon: 'mic',
        cap: 'recording',
      },
      {
        id: 'audio-extraction' as SettingsSection,
        label: t('settings.audioExtraction.title'),
        icon: 'file-audio',
        cap: 'audio_extraction',
      },
    ],
  };
}

/** Cloud edition: org-admin billing/usage/team, gated by audience='org_admin' so regular org members never see them. Community never reaches here (isCloudEdition is false), so the group is empty and drops out. */
function billingTeamVisible(ctx: SidebarContext): boolean {
  const { capState } = ctx;
  return (
    [
      'billing',
      'usage_dashboard',
      'organizations',
      'org.takedown',
      'org.audit_logs',
      'org.legal_hold',
    ].some((key) => orgAdminCapOn(capState, key)) || orgPrivacyOn(capState, ctx.orgRole)
  );
}

function billingTeamGroup(ctx: SidebarContext): RawSection {
  const { t, capState } = ctx;
  return {
    title: t('settings.sections.billingTeam'),
    items: [
      ...(orgAdminCapOn(capState, 'billing')
        ? [{ id: 'billing' as SettingsSection, label: t('billing.navLabel'), icon: 'credit-card' }]
        : []),
      ...(orgAdminCapOn(capState, 'usage_dashboard')
        ? [{ id: 'usage' as SettingsSection, label: t('usage.navLabel'), icon: 'activity' }]
        : []),
      ...(orgAdminCapOn(capState, 'organizations')
        ? [
            { id: 'team' as SettingsSection, label: t('team.navLabel'), icon: 'users' },
            {
              id: 'org-erasures' as SettingsSection,
              label: t('orgErasures.navLabel'),
              icon: 'trash',
            },
          ]
        : []),
      // issue #1253: the organization's own takedown queue. Server-enforced by the
      // `org.takedown` capability (unlike the platform queue's UI-only `admin.takedown`).
      ...(orgAdminCapOn(capState, 'org.takedown')
        ? [
            {
              id: 'org-takedown' as SettingsSection,
              label: t('settings.quarantine.orgNavLabel'),
              icon: 'shield-off',
            },
          ]
        : []),
      ...(orgAdminCapOn(capState, 'org.audit_logs')
        ? [
            {
              id: 'org-audit-logs' as SettingsSection,
              label: t('orgAudit.navLabel'),
              icon: 'clipboard',
            },
          ]
        : []),
      ...(orgAdminCapOn(capState, 'org.legal_hold')
        ? [{ id: 'org-holds' as SettingsSection, label: t('holds.navLabel'), icon: 'shield' }]
        : []),
      ...(orgPrivacyOn(capState, ctx.orgRole)
        ? [{ id: 'org-privacy' as SettingsSection, label: t('privacy.org.navLabel'), icon: 'lock' }]
        : []),
    ],
  };
}

/** Administration = who may do what, and how this deployment behaves. Listed for every admin; rows above the admin's tier render disabled (see sectionLocked()). */
function administrationGroup(ctx: SidebarContext): RawSection {
  const { t, tenancyMulti, pendingApprovalCount } = ctx;
  return {
    title: t('settings.sections.administration'),
    items: [
      {
        id: 'admin-users' as SettingsSection,
        label: t('settings.users.title'),
        icon: 'users',
        cap: 'users.local_admin',
        badge: pendingApprovalCount,
      },
      {
        id: 'authentication' as SettingsSection,
        label: t('settings.authentication.title'),
        icon: 'key',
        cap: 'auth.config_ui',
      },
      {
        id: 'audit-logs' as SettingsSection,
        label: t('settings.auditLog.navLabel'),
        icon: 'list',
        cap: 'audit.logs',
      },
      // issue #576: abuse/DMCA takedown review queue. `admin.takedown` is
      // declared in the backend capability maps but gates the NAV ENTRY only —
      // the endpoints are NOT capability-gated, since `require_capability`
      // 404s and a compliance endpoint that vanishes is worse than one that
      // refuses.
      {
        id: 'quarantine' as SettingsSection,
        label: t('settings.quarantine.navLabel'),
        icon: 'shield-off',
        cap: 'admin.takedown',
      },
      {
        id: 'takedown-notices' as SettingsSection,
        label: t('takedown.admin.navLabel'),
        icon: 'file-text',
        cap: 'admin.takedown',
      },
      // Operator legal holds without a takedown (legal-hold plan §8.4); ungated like the takedown routes.
      {
        id: 'platform-holds' as SettingsSection,
        label: t('holds.platform.navLabel'),
        icon: 'shield',
      },
      ...(tenancyMulti
        ? [
            {
              id: 'support-access' as SettingsSection,
              label: t('settings.supportAccess.navLabel'),
              icon: 'life-buoy',
            },
          ]
        : []),
    ],
  };
}

/**
 * System = looking at or repairing the deployment's state (vs. Administration, which configures
 * its behaviour). Last because it is the least-often opened. The GROUP is deliberately ungated:
 * `system-statistics` is open to every signed-in user and is the modal's default landing section
 * (settingsModalStore initialState / Navbar.svelte). Only the maintenance rows are admin-gated.
 */
function systemGroup(ctx: SidebarContext): RawSection {
  const { t, isAdmin, legalAvailable, dataAccess } = ctx;
  return {
    title: t('settings.sections.system'),
    items: [
      {
        id: 'system-statistics' as SettingsSection,
        label: t('settings.statistics.title'),
        icon: 'chart',
        cap: 'system.hardware_stats',
      },
      {
        id: 'file-status' as SettingsSection,
        label: t('settings.fileStatus.title'),
        icon: 'activity',
      },
      // Hidden when the deployment ships no legal documents (cloud edition) or the list fails.
      ...(legalAvailable
        ? [{ id: 'legal' as SettingsSection, label: t('settings.legal.title'), icon: 'file-text' }]
        : []),
      // Task health is the Recovery tab of File status (admin-task-health stays as an alias).
      ...(isAdmin
        ? [
            // File retention, media cache, backups, integrity, search index and speaker embeddings
            // are tabs of one row (the old ids stay as aliases; see lib/settings/sectionAliases.ts).
            ...(dataManagementVisible(dataAccess)
              ? [
                  {
                    id: 'data-management' as SettingsSection,
                    label: t('settings.dataManagement.title'),
                    icon: 'database',
                  },
                ]
              : []),
          ]
        : []),
    ],
  };
}

export function buildSidebarSections(ctx: SidebarContext) {
  const { capState, isAdmin, isSuperAdmin } = ctx;
  // Define sidebar sections (filtered by capability; empty sections drop out).
  //
  // Grouped by WHO changes it and HOW OFTEN, not by subject matter (issue #861):
  // the user's own settings first, then shared/media concerns, then admin
  // configuration, then deployment maintenance last. Within every group, rows are
  // ordered by expected frequency of use — do NOT re-alphabetize them.
  //
  // Group gating vs row gating: a group carrying `...(isAdmin ? [...] : [])` is
  // invisible below that tier. A row inside an UNGATED group that needs privilege
  // is spread in the same way, which keeps the group (and its ungated siblings)
  // visible. `SECTION_MIN_ROLE` (sectionAccess.ts) stays the single source of privilege truth —
  // never add an ad-hoc guard on a render block.
  const groups: RawSection[] = [
    accountGroup(ctx),
    transcriptionGroup(ctx),
    aiChatGroup(ctx),
    mediaGroup(ctx),
    ...(billingTeamVisible(ctx) ? [billingTeamGroup(ctx)] : []),
    ...(isAdmin ? [administrationGroup(ctx)] : []),
    systemGroup(ctx),
  ];
  return groups
    .map((section) => ({
      ...section,
      items: section.items
        .filter((item) => capOn(capState, item.cap))
        // A capability the deployment lacks is genuinely absent, so it stays
        // filtered out above. A privilege the *user* lacks is not: the entry
        // remains, flagged `locked`, and renders greyed out with a reason.
        // `badge` is normalised to a number for every item so the template can
        // read it without the union splitting on whether the property exists.
        .map((item) => ({
          ...item,
          locked: sectionLocked(item.id, isAdmin, isSuperAdmin),
          badge: item.badge ?? 0,
        })),
    }))
    .filter((section) => section.items.length > 0);
}
