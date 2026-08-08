<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import i18next from 'i18next';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { page } from '$app/stores';
  import { user as userStore } from '$stores/auth';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import { isCloudEdition } from '$lib/edition';
  import { settingsModalStore, type SettingsSection } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import axiosInstance from '$lib/axios';
  import { UserSettingsApi, RecordingSettingsHelper, type RecordingSettings } from '$lib/api/userSettings';

  // Import settings components
  import LLMSettings from '$components/settings/LLMSettings.svelte';
  import PromptSettings from '$components/settings/PromptSettings.svelte';
  import AudioExtractionSettings from '$components/settings/AudioExtractionSettings.svelte';
  import TranscriptionSettings from '$components/settings/TranscriptionSettings.svelte';
  import OrganizationContextSettings from '$components/settings/OrganizationContextSettings.svelte';
  import DownloadSettings from '$components/settings/DownloadSettings.svelte';
  import MediaSourcesSettings from '$components/settings/MediaSourcesSettings.svelte';
  import WatchSourcesSettings from '$components/settings/WatchSourcesSettings.svelte';
  import SearchSettings from '$components/settings/SearchSettings.svelte';
  import GroupsSettings from '$components/settings/GroupsSettings.svelte';
  import DataIntegritySettings from '$components/settings/DataIntegritySettings.svelte';
  import EmbeddingConsistencySettings from '$components/settings/EmbeddingConsistencySettings.svelte';
  import EmbeddingMigrationSettings from '$components/settings/EmbeddingMigrationSettings.svelte';
  import RetentionSettings from '$components/settings/RetentionSettings.svelte';
  import BackupSettings from '$components/settings/BackupSettings.svelte';
  import SpeakerAttributeSettings from '$components/settings/SpeakerAttributeSettings.svelte';
  import AutoLabelSettings from '$components/settings/AutoLabelSettings.svelte';
  import AuthenticationSettings from '$components/settings/AuthenticationSettings.svelte';
  import AccountStatusDashboard from '$components/settings/AccountStatusDashboard.svelte';
  import AuditLogViewer from '$components/settings/AuditLogViewer.svelte';
  import ASRSettings from '$components/settings/ASRSettings.svelte';
  import EngineSettings from '$components/settings/EngineSettings.svelte';
  import ContentRedactionSettings from '$components/settings/ContentRedactionSettings.svelte';
  import ChatSettingsPanel from '$components/settings/ChatSettingsPanel.svelte';
  import RedactionPolicySettings from '$components/settings/RedactionPolicySettings.svelte';
  import CustomVocabularySettings from '$components/settings/CustomVocabularySettings.svelte';
  import SystemStatisticsPanel from '$components/settings/SystemStatisticsPanel.svelte';
  import AdminTaskHealthPanel, { type ConfirmRequest } from '$components/settings/AdminTaskHealthPanel.svelte';
  import UserProfileSettings from '$components/settings/UserProfileSettings.svelte';
  // Managed-edition org-admin panels (only rendered when edition=cloud + org_admin;
  // community builds get inert stubs from the $lib/cloud seam)
  import BillingPanel from '$lib/cloud/components/BillingPanel.svelte';
  import UsageDashboard from '$lib/cloud/components/UsageDashboard.svelte';
  import TeamManagement from '$lib/cloud/components/TeamManagement.svelte';
  import UserManagementTable from '$components/UserManagementTable.svelte';
  import PendingApprovalsPanel from '$components/settings/PendingApprovalsPanel.svelte';
  import { UserApprovalsApi } from '$lib/api/userApprovals';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import ProcessingDetailsModal from '$components/settings/ProcessingDetailsModal.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';

  import SettingsSearch from '$components/settings/SettingsSearch.svelte';
  import {
    buildSettingsSearchItems,
    createSettingsFuzzyIndex,
    type SettingsSearchItem,
    type VisibleSection,
  } from '$lib/search/settingsSearchIndex';
  import type { FuzzyIndex } from '$lib/search/fuzzyMatcher';

  // Import i18n
  import { t, locale } from '$stores/locale';
  import { getErrorMessage, handleApiError } from '$lib/utils/apiError';

  // Modal state
  let modalElement: HTMLElement;
  let showCloseConfirmation = false;

  // Settings state
  $: isOpen = $settingsModalStore.isOpen;
  $: activeSection = $settingsModalStore.activeSection;

  // Settings search (macOS-style). Index is rebuilt when the visible-section set
  // (capabilities/edition) or the locale changes; desktop + mobile instances share it.
  let desktopSearchActive = false;
  let mobileSearchActive = false;
  let settingsSearchIndex: FuzzyIndex<SettingsSearchItem> | null = null;

  // Close modal when the user navigates to a different page
  let _prevPath = '';
  $: {
    const newPath = $page.url.pathname;
    if (isOpen && _prevPath && newPath !== _prevPath) {
      closeModal();
    }
    _prevPath = newPath;
  }
  $: isAdmin = $userStore?.role === 'admin' || $userStore?.role === 'super_admin';
  $: isSuperAdmin = $userStore?.role === 'super_admin';

  /**
   * The privilege tier each gated section needs, mirroring the FastAPI dependency
   * on the endpoints it calls (`get_current_admin_user` vs
   * `get_current_active_superuser`). This is the ONE place the modal decides who
   * may open what: the sidebar, the mobile picker and the content router all read
   * it, so a nav entry can no longer disagree with what the panel renders.
   *
   * Sections absent from the map are open to any signed-in user.
   */
  const SECTION_MIN_ROLE: Partial<Record<SettingsSection, 'admin' | 'super_admin'>> = {
    // super_admin — deployment configuration (P4.3 moved these off the admin tier)
    authentication: 'super_admin',
    'audit-logs': 'super_admin',
    backup: 'super_admin',
    'engine-settings': 'super_admin',
    'redaction-policy': 'super_admin',
    // admin
    'admin-users': 'admin',
    'admin-task-health': 'admin',
    'data-integrity': 'admin',
    'embedding-migration': 'admin',
    retention: 'admin',
    'search-indexing': 'admin',
  };

  /**
   * A section the user cannot open is rendered **disabled with a tooltip**, never
   * omitted. Silently dropping the entry is what made an admin conclude the LDAP
   * configuration page did not exist, so absence is no longer used to express
   * "not for you" — only greyed-out presence is.
   *
   * Below-admin users still don't see admin groups at all: those sit inside
   * `isAdmin`-gated sidebar groups, so there is no entry to grey out.
   */
  const sectionLocked = (
    section: SettingsSection,
    admin: boolean,
    superAdmin: boolean
  ): boolean => {
    const required = SECTION_MIN_ROLE[section];
    if (required === 'super_admin') return !superAdmin;
    if (required === 'admin') return !admin;
    return false;
  };

  $: activeSectionLocked = sectionLocked(activeSection, isAdmin, isSuperAdmin);
  $: activeSectionRequires = SECTION_MIN_ROLE[activeSection];

  /** Tooltip for a locked entry, worded for the tier it actually needs. */
  const lockReason = (section: SettingsSection): string =>
    SECTION_MIN_ROLE[section] === 'admin'
      ? $t('settings.permission.adminTitle')
      : $t('settings.nav.requiresSuperAdmin');

  // Recording settings section
  let maxRecordingDuration = 120;
  let recordingQuality: 'standard' | 'high' | 'maximum' = 'high';
  let autoStopEnabled = true;
  let recordingSettingsChanged = false;
  let recordingSettingsLoading = false;

  // Admin Users section
  let users: any[] = [];
  let usersLoading = false;

  /**
   * Accounts awaiting an approval decision. Owned here rather than in the panel
   * because the sidebar badge has to show it while the user is looking at some
   * other section — a queue nobody can see is a queue nobody works. Zero while
   * `require_account_approval` is off, which is when the badge disappears.
   *
   * A failed fetch leaves it at 0: an admin whose deployment 404s or 403s this
   * endpoint should see no badge, not a permanent phantom count.
   */
  let pendingApprovalCount = 0;

  async function loadPendingApprovalCount() {
    try {
      pendingApprovalCount = (await UserApprovalsApi.list()).length;
    } catch {
      pendingApprovalCount = 0;
    }
  }

  // Admin Stats section
  let stats: any = {
    users: { total: 0, new: 0 },
    files: { total: 0, new: 0, total_duration: 0, segments: 0 },
    tasks: {
      total: 0,
      pending: 0,
      running: 0,
      completed: 0,
      failed: 0,
      success_rate: 0,
      avg_processing_time: 0,
      recent: []
    },
    speakers: { total: 0, avg_per_file: 0 },
    models: {
      whisper: { name: $t('common.notAvailable'), description: $t('common.notAvailable'), purpose: $t('common.notAvailable') },
      diarization: { name: $t('common.notAvailable'), description: $t('common.notAvailable'), purpose: $t('common.notAvailable') }
    },
    system: {
      cpu: { total_percent: '0%', per_cpu: [], logical_cores: 0, physical_cores: 0 },
      memory: { total: '0 B', available: '0 B', used: '0 B', percent: '0%' },
      disk: { total: '0 B', used: '0 B', free: '0 B', percent: '0%' },
      // These stay the untranslated literal "N/A" on purpose: it is the backend's
      // wire sentinel for an unreadable GPU metric (admin.py / tasks/utility.py) and
      // SystemStatisticsPanel compares `utilization_percent !== 'N/A'` against it.
      // The placeholder entry is `available: false`, so none of it ever renders.
      gpus: [{ available: false, name: 'N/A', memory_total: 'N/A', memory_used: 'N/A', memory_free: 'N/A', memory_percent: 'N/A', utilization_percent: 'N/A', temperature_celsius: null }],
      uptime: $t('common.unknown'),
      platform: $t('common.unknown'),
      python_version: $t('common.unknown')
    },
    throughput: { total_completed: 0, last_1h: 0, last_3h: 0, rate_1h: 0, rate_3h: 0 },
    eta: { remaining: 0, files_per_hour: 0, hours_remaining: null, est_completion: null },
    file_timing: { files: 0, avg_secs: 0, min_secs: 0, max_secs: 0, avg_mins: 0 },
    queues: { gpu: 0, download: 0, nlp: 0, embedding: 0, cpu: 0, utility: 0, total: 0 }
  };
  let showProcessingDetails = false;
  let processingDetailsSection = 'performance';
  let statsLoading = false;
  let statsRefreshing = false;
  let statsInitialLoaded = false;
  let gpuRetryScheduled = false;

  // Search index status (for system stats card)
  let searchIndexStatus: { indexed_files: number; total_files: number; pending_files: number; in_progress: boolean; current_model: string } | null = null;
  let searchHealthStatus: Record<string, { status: string; doc_count: number }> | null = null;

  // Admin Task Health confirmation flow (panel dispatches, modal owns ConfirmationModal)
  let showConfirmModal = false;
  let confirmModalTitle = '';
  let confirmModalMessage = '';
  let confirmCallback: (() => void) | null = null;

  // Edition capability gating (server-driven entitlements): a section item
  // tagged with `cap` renders only when the backend lists that capability as
  // enabled for this deployment. Community = everything on (no change);
  // cloud hides platform/self-host surfaces so the product "just works".
  // The backend independently 404s gated endpoints — this is cosmetic only.
  $: capState = $capabilities;
  const capOn = (state: typeof $capabilities, key?: string) =>
    !key || isCapabilityEnabled(state, key);

  // Cloud-edition org-admin gating: the new billing/usage/team panels are only
  // surfaced when the backend marks their capability as enabled AND audience as
  // 'org_admin' (so regular org members don't see billing controls). The
  // capability must be enabled too — a disabled cap hides the panel entirely.
  const orgAdminCapOn = (state: typeof $capabilities, key: string) =>
    isCloudEdition && isCapabilityEnabled(state, key) && state.audience[key] === 'org_admin';

  // Define sidebar sections (filtered by capability; empty sections drop out)
  $: sidebarSections = [
    {
      title: $t('settings.sections.system'),
      items: [
        { id: 'system-statistics' as SettingsSection, label: $t('settings.statistics.title'), icon: 'chart', cap: 'system.hardware_stats' }
      ]
    },
    // Cloud edition — org-admin billing/usage/team. Gated by audience='org_admin'
    // so regular org members never see these. Community never reaches here
    // (isCloudEdition is false), so the section is empty and drops out.
    ...(orgAdminCapOn(capState, 'billing') || orgAdminCapOn(capState, 'usage_dashboard') || orgAdminCapOn(capState, 'organizations') ? [
      {
        title: $t('settings.sections.billingTeam'),
        items: [
          ...(orgAdminCapOn(capState, 'billing') ? [{ id: 'billing' as SettingsSection, label: $t('billing.navLabel'), icon: 'credit-card' }] : []),
          ...(orgAdminCapOn(capState, 'usage_dashboard') ? [{ id: 'usage' as SettingsSection, label: $t('usage.navLabel'), icon: 'activity' }] : []),
          ...(orgAdminCapOn(capState, 'organizations') ? [{ id: 'team' as SettingsSection, label: $t('team.navLabel'), icon: 'users' }] : [])
        ]
      }
    ] : []),
    ...(isAdmin ? [
      {
        title: $t('settings.sections.administration'),
        items: [
          // Listed for every admin, disabled for non-super_admins — see sectionLocked().
          { id: 'audit-logs' as SettingsSection, label: $t('settings.auditLog.navLabel'), icon: 'list', cap: 'audit.logs' },
          { id: 'authentication' as SettingsSection, label: $t('settings.authentication.title'), icon: 'key', cap: 'auth.config_ui' },
          { id: 'admin-users' as SettingsSection, label: $t('settings.users.title'), icon: 'users', cap: 'users.local_admin', badge: pendingApprovalCount }
        ]
      },
      {
        title: $t('settings.sections.systemManagement'),
        items: [
          { id: 'data-integrity' as SettingsSection, label: $t('settings.dataIntegrity.title'), icon: 'shield', cap: 'admin.data_integrity' },
          { id: 'retention' as SettingsSection, label: $t('settings.retention.title'), icon: 'clock', cap: 'admin.retention' },
          { id: 'backup' as SettingsSection, label: $t('settings.backup.title'), icon: 'database', cap: 'admin.backup' },
          { id: 'search-indexing' as SettingsSection, label: $t('settings.searchIndexing.title'), icon: 'search', cap: 'admin.search_indexing' },
          { id: 'embedding-migration' as SettingsSection, label: $t('settings.embeddingMigration.title'), icon: 'database', cap: 'admin.embedding_migration' },
          { id: 'admin-task-health' as SettingsSection, label: $t('settings.taskHealth.title'), icon: 'health', cap: 'admin.task_health' }
        ]
      }
    ] : []),
    {
      title: $t('settings.sections.account'),
      items: [
        { id: 'groups' as SettingsSection, label: $t('groups.title'), icon: 'group', cap: 'sharing.teams' },
        { id: 'profile' as SettingsSection, label: $t('settings.profile.title'), icon: 'user' }
      ]
    },
    {
      title: $t('settings.sections.transcriptionAi'),
      items: [
        { id: 'ai-prompts' as SettingsSection, label: $t('settings.aiPrompts.title'), icon: 'message', cap: 'prompts.user' },
        { id: 'asr-provider' as SettingsSection, label: $t('settings.asrProvider.title'), icon: 'mic', cap: 'asr.user_providers' },
        ...(isAdmin ? [{ id: 'engine-settings' as SettingsSection, label: $t('settings.engineSettings.title'), icon: 'cpu', cap: 'engine.settings' }] : []),
        ...(isAdmin ? [{ id: 'redaction-policy' as SettingsSection, label: $t('settings.redactionPolicy.title'), icon: 'shield', cap: 'redaction.policy' }] : []),
        { id: 'auto-labeling' as SettingsSection, label: $t('autoLabel.title'), icon: 'tag' },
        { id: 'custom-vocabulary' as SettingsSection, label: $t('settings.customVocabulary.title'), icon: 'list', cap: 'vocab.user' },
        { id: 'content-redaction' as SettingsSection, label: $t('settings.contentRedaction.title'), icon: 'eye-off', cap: 'redaction.user' },
        // Admin platform tuning lives in this panel's Advanced tab, not a second row.
        { id: 'chat' as SettingsSection, label: $t('chat.settings.title'), icon: 'message', cap: 'chat.rag' },
        { id: 'llm-provider' as SettingsSection, label: $t('settings.llmProvider.title'), icon: 'brain', cap: 'llm.user_settings' },
        { id: 'organization-context' as SettingsSection, label: isCloudEdition ? $t('settings.orgContext.cloudTitle') : $t('settings.orgContext.title'), icon: 'briefcase' },
        { id: 'speaker-attributes' as SettingsSection, label: $t('settings.speakerAttributes.navTitle'), icon: 'user' },
        { id: 'transcription' as SettingsSection, label: $t('settings.transcription.title'), icon: 'waveform', cap: 'transcription.prefs' }
      ]
    },
    {
      title: $t('settings.sections.mediaOutput'),
      items: [
        { id: 'audio-extraction' as SettingsSection, label: $t('settings.audioExtraction.title'), icon: 'file-audio' },
        { id: 'media-sources' as SettingsSection, label: $t('settings.mediaSources.title'), icon: 'link' },
        { id: 'watch-sources' as SettingsSection, label: $t('settings.watchSources.title'), icon: 'eye', cap: 'watch_sources' },
        { id: 'recording' as SettingsSection, label: $t('settings.recording.title'), icon: 'mic', cap: 'recording' },
        { id: 'download' as SettingsSection, label: $t('settings.download.title'), icon: 'download', cap: 'exports' }
      ]
    }
  ]
    .map((section) => ({
      ...section,
      items: section.items
        .filter((item) => capOn(capState, (item as { cap?: string }).cap))
        // A capability the deployment lacks is genuinely absent, so it stays
        // filtered out above. A privilege the *user* lacks is not: the entry
        // remains, flagged `locked`, and renders greyed out with a reason.
        // `badge` is normalised to a number for every item so the template can
        // read it without the union splitting on whether the property exists.
        .map((item) => ({
          ...item,
          locked: sectionLocked(item.id, isAdmin, isSuperAdmin),
          badge: (item as { badge?: number }).badge ?? 0
        }))
    }))
    .filter((section) => section.items.length > 0);

  // Flat list of the sections this user can actually open — the search index is
  // limited to these so it never surfaces a section the user can't navigate to.
  // Locked entries are excluded on purpose: they stay discoverable in the sidebar
  // (greyed out, with a tooltip), but a search hit promises a usable destination.
  $: visibleSections = sidebarSections.flatMap((section) =>
    section.items
      .filter((item) => !item.locked)
      .map((item) => ({ id: item.id, label: item.label }) as VisibleSection)
  );

  // Rebuild the fuzzy index only while the modal is open. `$locale` is referenced
  // so the index refreshes when the UI language changes (labels are localized).
  $: settingsSearchIndex = isOpen ? buildSettingsFuzzyIndex($locale, visibleSections) : null;

  function buildSettingsFuzzyIndex(_locale: string, sections: VisibleSection[]) {
    if (!i18next.isInitialized) return null;
    const dict = i18next.getResourceBundle(i18next.language, 'translation') as Record<
      string,
      string
    > | null;
    const items = buildSettingsSearchItems(dict || {}, sections);
    return createSettingsFuzzyIndex(items);
  }

  async function handleSettingsNavigate(
    event: CustomEvent<{ sectionId: SettingsSection; anchorText: string }>
  ) {
    const { sectionId, anchorText } = event.detail;
    switchSection(sectionId);
    // Let the panel render (some load data async — flashSettingControl retries).
    await tick();
    flashSettingControl(anchorText);
  }

  // Tiered lookup: prefer labels/headings, then help text, then any element.
  function findSettingElement(container: Element, needle: string): HTMLElement | null {
    const target = needle.trim().toLowerCase();
    if (!target) return null;
    const tiers = [
      'label, .section-title, .section-header, h3, h4, h5, legend',
      '.form-text, .field-hint, .help-text, .field-desc, .input-hint, small, p',
      '*',
    ];
    for (const selector of tiers) {
      for (const el of Array.from(container.querySelectorAll(selector))) {
        const text = (el.textContent || '').trim().toLowerCase();
        if (text && text.includes(target)) return el as HTMLElement;
      }
    }
    return null;
  }

  function flashSettingControl(anchorText: string, attempt = 0) {
    if (!anchorText) return;
    const container = modalElement?.querySelector('.settings-content');
    const el = container ? findSettingElement(container, anchorText) : null;
    if (!el) {
      // Panel content may still be loading — retry briefly (~1s total) before giving up.
      if (attempt < 20) window.setTimeout(() => flashSettingControl(anchorText, attempt + 1), 50);
      return;
    }
    const scrollTarget =
      (el.closest(
        '.form-group, .form-row, .form-field, .settings-section, .content-section'
      ) as HTMLElement) || el;
    scrollTarget.scrollIntoView({ behavior: 'smooth', block: 'center' });
    document
      .querySelectorAll('.settings-search-flash')
      .forEach((node) => node.classList.remove('settings-search-flash'));
    scrollTarget.classList.add('settings-search-flash');
    window.setTimeout(() => scrollTarget.classList.remove('settings-search-flash'), 1600);
  }

  // Reactive recording settings change detection
  $: {
    settingsModalStore.setDirty('recording', recordingSettingsChanged);
  }

  onMount(() => {
    // Load recording settings
    loadRecordingSettings();

    // Load statistics for any user
    if (activeSection === 'system-statistics') {
      loadStats();
    }

    // Load admin data if admin
    if (isAdmin) {
      // The count drives the sidebar badge, so it is fetched regardless of which
      // section is open.
      loadPendingApprovalCount();
      if (activeSection === 'admin-users') {
        loadAdminUsers();
      }
    }

    // Add escape key listener
    document.addEventListener('keydown', handleKeyDown);
    window.addEventListener('gpu-stats-updated', handleGpuStatsEvent);
    window.addEventListener('reindex-complete', handleReindexCompleteStats);
  });

  onDestroy(() => {
    document.removeEventListener('keydown', handleKeyDown);
    window.removeEventListener('gpu-stats-updated', handleGpuStatsEvent);
    window.removeEventListener('reindex-complete', handleReindexCompleteStats);
    if (previousOpenState) unlockScroll();
  });

  // Track previous open state to detect when modal opens
  let previousOpenState = false;

  // Prevent body scroll when modal is open and load initial data
  $: {
    if (isOpen && !previousOpenState) {
      // Modal just opened — prevent background scroll
      lockScroll();

      // Load data for the active section when modal opens.
      // admin-task-health self-loads on its panel's mount.
      if (activeSection === 'system-statistics') {
        loadStats();
      } else if (activeSection === 'admin-users' && isAdmin) {
        loadAdminUsers();
      }
      if (isAdmin) loadPendingApprovalCount();

      previousOpenState = true;
    } else if (!isOpen && previousOpenState) {
      // Modal just closed — restore background scroll
      unlockScroll();
      previousOpenState = false;
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === 'Escape' && isOpen) {
      attemptClose();
    }
  }

  function handleBackdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) {
      attemptClose();
    }
  }

  function attemptClose() {
    const hasUnsavedChanges = settingsModalStore.hasAnyDirty($settingsModalStore);
    if (hasUnsavedChanges) {
      showCloseConfirmation = true;
    } else {
      closeModal();
    }
  }

  function closeModal() {
    settingsModalStore.close();
    showCloseConfirmation = false;
    resetAllForms();
  }

  function forceClose() {
    showCloseConfirmation = false;
    closeModal();
  }

  function resetAllForms() {
    // Profile & password fields live in UserProfileSettings, which unmounts when
    // the modal closes and re-initializes from authStore on its next mount.

    // Reset recording settings
    loadRecordingSettings();

    // Clear all dirty states
    settingsModalStore.clearAllDirty();
  }

  function handleGpuStatsEvent(event: Event) {
    const gpuData = (event as CustomEvent).detail;
    if (gpuData && stats?.system) {
      const gpus = Array.isArray(gpuData) ? gpuData : [gpuData];
      // SystemStatisticsPanel clamps its own GPU index reactively on count change.
      stats = { ...stats, system: { ...stats.system, gpus } };
    }
  }

  function switchSection(sectionId: SettingsSection) {
    settingsModalStore.setActiveSection(sectionId);

    // Load data for specific sections on navigation.
    // GPU stats refresh via WebSocket broadcast (every 5 min) — no polling needed.
    if (sectionId === 'system-statistics') {
      loadStats();
    } else if (sectionId === 'admin-users') {
      loadAdminUsers();
    }
    // admin-task-health self-loads on its panel's mount.
  }

  // Recording settings functions
  async function loadRecordingSettings() {
    recordingSettingsLoading = true;
    try {
      const settings = await UserSettingsApi.getRecordingSettings();
      maxRecordingDuration = settings.max_recording_duration;
      recordingQuality = settings.recording_quality;
      autoStopEnabled = settings.auto_stop_enabled;
      recordingSettingsChanged = false;
    } catch (err: unknown) {
      console.error('Error loading recording settings:', err);
      // Via handleApiError, not a bare toast: this modal mounts before the first
      // account-lifecycle 403 lands, so a pending-approval user got a raw
      // "Request failed with status code 403" toast in front of the blocking
      // screen. handleApiError swallows lifecycle refusals the way it already
      // swallows cancellations.
      handleApiError(err, $t('settings.toast.recordingSettingsSaveFailed'));
    } finally {
      recordingSettingsLoading = false;
    }
  }

  function handleRecordingSettingsChange() {
    recordingSettingsChanged = true;
    settingsModalStore.setDirty('recording', true);
  }

  async function saveRecordingSettings() {
    recordingSettingsLoading = true;

    // Validate settings
    const settingsToValidate: RecordingSettings = {
      max_recording_duration: maxRecordingDuration,
      recording_quality: recordingQuality,
      auto_stop_enabled: autoStopEnabled
    };

    const validationErrors = RecordingSettingsHelper.validateSettings(settingsToValidate);
    if (validationErrors.length > 0) {
      toastStore.error(validationErrors[0]);
      recordingSettingsLoading = false;
      return;
    }

    try {
      await UserSettingsApi.updateRecordingSettings(settingsToValidate);
      toastStore.success($t('settings.toast.recordingSettingsSaved'));
      recordingSettingsChanged = false;
      settingsModalStore.clearDirty('recording');
    } catch (err: unknown) {
      console.error('Error saving recording settings:', err);
      const message = getErrorMessage(err, $t('settings.toast.recordingSettingsSaveFailed'));
      toastStore.error(message);
    } finally {
      recordingSettingsLoading = false;
    }
  }

  async function resetRecordingSettings() {
    recordingSettingsLoading = true;

    try {
      await UserSettingsApi.resetRecordingSettings();
      await loadRecordingSettings();
      toastStore.success($t('settings.toast.recordingSettingsReset'));
      recordingSettingsChanged = false;
      settingsModalStore.clearDirty('recording');
    } catch (err: unknown) {
      console.error('Error resetting recording settings:', err);
      const message = getErrorMessage(err, $t('settings.toast.recordingSettingsResetFailed'));
      toastStore.error(message);
    } finally {
      recordingSettingsLoading = false;
    }
  }

  // Admin functions
  async function loadAdminUsers(showLoading = true) {
    // Only show loading spinner on initial load, not on refresh
    if (showLoading) {
      usersLoading = true;
    }

    try {
      const response = await axiosInstance.get('/admin/users');
      users = response.data;
    } catch (err: unknown) {
      console.error('Error loading admin users:', err);
      const message = getErrorMessage(err, $t('settings.toast.usersLoadFailed'));
      toastStore.error(message);
    } finally {
      if (showLoading) {
        usersLoading = false;
      }
    }
  }

  async function refreshAdminUsers() {
    // Silent refresh - don't show loading spinner to reduce flicker
    await loadAdminUsers(false);
  }

  async function recoverUserFiles(userId: string) {
    try {
      await axiosInstance.post(`/tasks/system/recover-user-files/${userId}`);
      toastStore.success($t('settings.toast.userRecoveryInitiated'));
    } catch (err: unknown) {
      console.error('Error recovering user files:', err);
      const message = getErrorMessage(err, $t('settings.toast.userRecoveryFailed'));
      toastStore.error(message);
    }
  }

  async function loadStats() {
    if (statsInitialLoaded) {
      statsRefreshing = true;
    } else {
      statsLoading = true;
    }

    try {
      const [statsRes, indexRes, healthRes] = await Promise.all([
        axiosInstance.get('/system/stats'),
        axiosInstance.get('/search/reindex/status').catch(() => null),
        axiosInstance.get('/search/index-health').catch(() => null),
      ]);

      stats = statsRes.data;
      statsInitialLoaded = true;

      if (indexRes?.data) searchIndexStatus = indexRes.data;
      if (healthRes?.data) searchHealthStatus = healthRes.data;

      // Auto-retry once if GPU stats are loading
      if (statsRes.data?.system?.gpus?.[0]?.loading && !gpuRetryScheduled) {
        gpuRetryScheduled = true;
        setTimeout(() => {
          gpuRetryScheduled = false;
          loadStats();
        }, 5000);
      }
    } catch (err: unknown) {
      console.error('Error loading stats:', err);
      const message = getErrorMessage(err, $t('settings.toast.statisticsLoadFailed'));
      toastStore.error(message);
    } finally {
      statsLoading = false;
      statsRefreshing = false;
    }
  }

  async function refreshStats() {
    await loadStats();
  }

  function handleReindexCompleteStats() {
    // Refresh system stats after reindex completes (e.g., new embedding model)
    if (statsInitialLoaded) {
      loadStats();
    }
  }

  function openProcessingDetails(section: string) {
    processingDetailsSection = section;
    showProcessingDetails = true;
  }

  function showConfirmation(title: string, message: string, callback: () => void) {
    confirmModalTitle = title;
    confirmModalMessage = message;
    confirmCallback = callback;
    showConfirmModal = true;
  }

  function handleTaskHealthConfirm(event: CustomEvent<ConfirmRequest>) {
    const { title, message, callback } = event.detail;
    showConfirmation(title, message, callback);
  }

  function handleConfirmModalConfirm() {
    showConfirmModal = false;
    if (confirmCallback) {
      confirmCallback();
      confirmCallback = null;
    }
  }

  function handleConfirmModalCancel() {
    showConfirmModal = false;
    confirmCallback = null;
  }

  // AI settings change handlers
  function onAISettingsChange() {
    // Handler for AI settings changes - can be extended for additional logic
  }
</script>

{#if isOpen}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="settings-modal-backdrop"
    on:click={handleBackdropClick}
    on:wheel|preventDefault|self
    on:touchmove|preventDefault|self
    role="presentation"
  >
    <div class="settings-modal" bind:this={modalElement} role="dialog" aria-modal="true" aria-labelledby="settings-modal-title">
      <!-- Header bar with title and close button -->
      <div class="settings-header-bar">
        <h2 id="settings-modal-title" class="settings-header-title">{$t('settings.title')}</h2>
        <button class="modal-close-button" on:click={attemptClose} aria-label={$t('settings.modal.closeSettings')} title={$t('settings.modal.closeSettingsTitle')} style="position:static; margin:0; padding:0.5rem;">
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>

      <div class="settings-modal-container">
        <!-- Desktop sidebar -->
        <aside class="settings-sidebar">
          <SettingsSearch
            index={settingsSearchIndex}
            idPrefix="settings-search-desktop"
            bind:active={desktopSearchActive}
            on:navigate={handleSettingsNavigate}
          />
          {#if !desktopSearchActive}
            {#each sidebarSections as section}
              <div class="sidebar-section">
                <h3 class="section-heading">{section.title}</h3>
                <nav class="section-nav">
                  {#each section.items as item}
                    <button
                      class="nav-item"
                      class:active={activeSection === item.id}
                      class:dirty={$settingsModalStore.dirtyState[item.id]}
                      class:locked={item.locked}
                      disabled={item.locked}
                      title={item.locked ? lockReason(item.id) : undefined}
                      on:click={() => switchSection(item.id)}
                    >
                      <span class="nav-item-label">{item.label}</span>
                      {#if !item.locked && item.badge > 0}
                        <span class="nav-badge" title={$t('settings.approvals.badgeTooltip')}>
                          {item.badge}
                        </span>
                      {/if}
                      {#if item.locked}
                        <span class="lock-indicator" aria-hidden="true">
                          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                            <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
                            <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
                          </svg>
                        </span>
                      {:else if $settingsModalStore.dirtyState[item.id]}
                        <span class="dirty-indicator" title={$t('settings.unsavedChanges')}>●</span>
                      {/if}
                    </button>
                  {/each}
                </nav>
              </div>
            {/each}
          {/if}
        </aside>

        <!-- Content Area -->
        <main class="settings-content">
          <!-- Mobile section navigation (hidden on desktop where sidebar is visible) -->
          <div class="mobile-section-nav">
            <SettingsSearch
              index={settingsSearchIndex}
              idPrefix="settings-search-mobile"
              bind:active={mobileSearchActive}
              on:navigate={handleSettingsNavigate}
            />
            {#if !mobileSearchActive}
              <!-- svelte-ignore a11y_label_has_associated_control -->
              <label class="mobile-nav-label">{$t('settings.title')}</label>
              <select
                class="mobile-nav-select"
                value={activeSection}
                on:change={(e) => switchSection(e.currentTarget.value as SettingsSection)}
              >
                {#each sidebarSections as section}
                  <optgroup label={section.title}>
                    {#each section.items as item}
                      <option value={item.id} disabled={item.locked}>
                        {item.locked
                          ? `${item.label} — ${lockReason(item.id)}`
                          : item.badge > 0
                            ? `${item.label} (${item.badge})`
                            : item.label}
                      </option>
                    {/each}
                  </optgroup>
                {/each}
              </select>
            {/if}
          </div>

          {#if activeSectionLocked}
            <!-- A deep link (or a demotion mid-session) can land on a section this
                 user may not open. Say so explicitly — the old behaviour was to
                 render nothing, which looked like a broken blank pane. -->
            <div class="content-section">
              <EmptyState
                icon="🔒"
                title={activeSectionRequires === 'super_admin'
                  ? $t('settings.permission.superAdminTitle')
                  : $t('settings.permission.adminTitle')}
                description={activeSectionRequires === 'super_admin'
                  ? $t('settings.permission.superAdminMessage')
                  : $t('settings.permission.adminMessage')}
              />
            </div>
          {:else}
          <!-- Profile Section -->
          {#if activeSection === 'profile'}
            <UserProfileSettings />
          {/if}

          <!-- Recording Settings Section -->
          {#if activeSection === 'recording'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.recording.title')}</h3>
              <p class="section-description">{$t('settings.recording.description')}</p>

              <form on:submit|preventDefault={saveRecordingSettings} class="settings-form">
                <div class="form-group">
                  <label for="maxRecordingDuration">{$t('settings.recording.maxDuration')}</label>
                  <input
                    type="number"
                    id="maxRecordingDuration"
                    class="form-control"
                    bind:value={maxRecordingDuration}
                    on:input={handleRecordingSettingsChange}
                    min="15"
                    max="480"
                    required
                  />
                  <small class="form-text">{$t('settings.recording.durationRange')}</small>
                </div>

                <div class="form-group">
                  <label for="recordingQuality">{$t('settings.recording.quality')}</label>
                  <select
                    id="recordingQuality"
                    class="form-control"
                    bind:value={recordingQuality}
                    on:change={handleRecordingSettingsChange}
                  >
                    <option value="standard">{$t('settings.recording.qualityStandard')}</option>
                    <option value="high">{$t('settings.recording.qualityHigh')}</option>
                    <option value="maximum">{$t('settings.recording.qualityMaximum')}</option>
                  </select>
                  <small class="form-text">{$t('settings.recording.qualityHelp')}</small>
                </div>

                <div class="form-group">
                  <label class="checkbox-label">
                    <input
                      type="checkbox"
                      bind:checked={autoStopEnabled}
                      on:change={handleRecordingSettingsChange}
                    />
                    <span>{$t('settings.recording.autoStop')}</span>
                  </label>
                  <small class="form-text">{$t('settings.recording.autoStopHelp')}</small>
                </div>

                <div class="form-actions">
                  <button
                    type="button"
                    class="btn btn-secondary"
                    on:click={resetRecordingSettings}
                    disabled={recordingSettingsLoading}
                  >
                    {$t('common.resetToDefaults')}
                  </button>

                  <button
                    type="submit"
                    class="btn btn-primary"
                    disabled={!recordingSettingsChanged || recordingSettingsLoading}
                  >
                    {recordingSettingsLoading ? $t('common.saving') : $t('common.saveSettings')}
                  </button>
                </div>
              </form>
            </div>
          {/if}

          <!-- Audio Extraction Settings Section -->
          {#if activeSection === 'audio-extraction'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.audioExtraction.title')}</h3>
              <p class="section-description">{$t('settings.audioExtraction.description')}</p>
              <AudioExtractionSettings />
            </div>
          {/if}

          <!-- Transcription Settings Section -->
          {#if activeSection === 'transcription'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.transcription.title')}</h3>
              <p class="section-description">{$t('settings.transcription.description')}</p>
              <TranscriptionSettings />
            </div>
          {/if}

          <!-- Organization Context Section -->
          {#if activeSection === 'organization-context'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.orgContext.title')}</h3>
              <p class="section-description">{$t('settings.orgContext.description')}</p>
              <OrganizationContextSettings />
            </div>
          {/if}

          <!-- Speaker Attribute Settings Section -->
          {#if activeSection === 'speaker-attributes'}
            <div class="content-section">
              <SpeakerAttributeSettings />
            </div>
          {/if}

          <!-- Download Settings Section -->
          {#if activeSection === 'download'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.download.title')}</h3>
              <p class="section-description">{$t('settings.download.description')}</p>
              <DownloadSettings />
            </div>
          {/if}

          <!-- Media Sources Settings Section -->
          {#if activeSection === 'media-sources'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.mediaSources.title')}</h3>
              <p class="section-description">{$t('settings.mediaSources.description')}</p>
              <MediaSourcesSettings />
            </div>
          {/if}

          <!-- Watch Sources Settings Section -->
          {#if activeSection === 'watch-sources'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.watchSources.title')}</h3>
              <p class="section-description">{$t('settings.watchSources.description')}</p>
              <WatchSourcesSettings />
            </div>
          {/if}

          <!-- AI Prompts Section -->
          {#if activeSection === 'ai-prompts'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.aiPrompts.title')}</h3>
              <p class="section-description">{$t('settings.aiPrompts.description')}</p>
              <PromptSettings onSettingsChange={onAISettingsChange} />
            </div>
          {/if}

          <!-- LLM Provider Section -->
          {#if activeSection === 'llm-provider'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.llmProvider.title')}</h3>
              <p class="section-description">{$t('settings.llmProvider.description')}</p>
              <LLMSettings onSettingsChange={onAISettingsChange} {isAdmin} />
            </div>
          {/if}

          <!-- Auto-Labeling Section -->
          {#if activeSection === 'auto-labeling'}
            <div class="content-section">
              <h3 class="section-title">{$t('autoLabel.title')}</h3>
              <p class="section-description">{$t('autoLabel.description')}</p>
              <AutoLabelSettings />
            </div>
          {/if}

          <!-- ASR Provider Section -->
          {#if activeSection === 'asr-provider'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.asrProvider.sectionTitle')}</h3>
              <p class="section-description">{$t('settings.asrProvider.description')}</p>
              <ASRSettings {isAdmin} />
            </div>
          {/if}

          <!-- Engine Configuration Section (admin only) -->
          {#if activeSection === 'engine-settings'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.engineSettings.title')}</h3>
              <p class="section-description">{$t('settings.engineSettings.description')}</p>
              <EngineSettings />
            </div>
          {/if}

          <!-- Custom Vocabulary Section -->
          {#if activeSection === 'custom-vocabulary'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.customVocabulary.title')}</h3>
              <p class="section-description">{$t('settings.customVocabulary.description')}</p>
              <CustomVocabularySettings />
            </div>
          {/if}

          <!-- Content Redaction Section (per-user, all users) -->
          {#if activeSection === 'content-redaction'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.contentRedaction.title')}</h3>
              <ContentRedactionSettings />
            </div>
          {/if}

          <!-- Chat: per-user defaults + (admin) platform tuning, as tabs.
               'chat-admin' is kept as a section id so the settings search and any
               existing deep link still resolve — it just opens the Advanced tab
               of the same panel instead of a second sidebar entry. -->
          {#if activeSection === 'chat' || activeSection === 'chat-admin'}
            <div class="content-section">
              <h3 class="section-title">{$t('chat.settings.title')}</h3>
              <ChatSettingsPanel
                {isAdmin}
                initialTab={activeSection === 'chat-admin' && isAdmin ? 'advanced' : 'general'}
              />
            </div>
          {/if}

          <!-- Redaction Policy Section (admin governance) -->
          {#if activeSection === 'redaction-policy'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.redactionPolicy.title')}</h3>
              <RedactionPolicySettings />
            </div>
          {/if}

          <!-- Search & Indexing Section -->
          {#if activeSection === 'search-indexing'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.searchIndexing.title')}</h3>
              <p class="section-description">{$t('settings.searchIndexing.description')}</p>
              <SearchSettings />
            </div>
          {/if}

          <!-- Groups Section -->
          {#if activeSection === 'groups'}
            <div class="content-section">
              <GroupsSettings />
            </div>
          {/if}

          <!-- Admin Users Section -->
          {#if activeSection === 'admin-users'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.users.title')}</h3>
              <p class="section-description">{$t('settings.users.description')}</p>
              {#if isSuperAdmin}
                <AccountStatusDashboard />
              {/if}
              <!-- Above the table on purpose: a queue is work waiting, and the
                   accounts in it are not in the table's list until they are
                   admitted. `onDecided` refreshes the user list so an approved
                   account appears there in the same interaction. -->
              <PendingApprovalsPanel
                onDecided={refreshAdminUsers}
                on:countchange={(e) => (pendingApprovalCount = e.detail)}
              />
              <UserManagementTable
                {users}
                loading={usersLoading}
                onRefresh={refreshAdminUsers}
                onUserRecovery={recoverUserFiles}
              />
            </div>
          {/if}

          <!-- System Statistics Section -->
          {#if activeSection === 'system-statistics'}
            <SystemStatisticsPanel
              {stats}
              {statsLoading}
              {statsRefreshing}
              {searchIndexStatus}
              {searchHealthStatus}
              on:refresh={refreshStats}
              on:openDetails={(e) => openProcessingDetails(e.detail)}
            />
          {/if}

          <!-- Admin Task Health Section -->
          {#if activeSection === 'admin-task-health'}
            <AdminTaskHealthPanel on:requestConfirm={handleTaskHealthConfirm} />
          {/if}

          <!-- Cloud edition — Billing (org admins only) -->
          {#if activeSection === 'billing' && orgAdminCapOn(capState, 'billing')}
            <div class="content-section">
              <h3 class="section-title">{$t('billing.title')}</h3>
              <p class="section-description">{$t('billing.description')}</p>
              <BillingPanel />
            </div>
          {/if}

          <!-- Cloud edition — Usage Dashboard (org admins only) -->
          {#if activeSection === 'usage' && orgAdminCapOn(capState, 'usage_dashboard')}
            <div class="content-section">
              <h3 class="section-title">{$t('usage.title')}</h3>
              <p class="section-description">{$t('usage.description')}</p>
              <UsageDashboard />
            </div>
          {/if}

          <!-- Cloud edition — Team Management (org admins only) -->
          {#if activeSection === 'team' && orgAdminCapOn(capState, 'organizations')}
            <div class="content-section">
              <h3 class="section-title">{$t('team.title')}</h3>
              <p class="section-description">{$t('team.description')}</p>
              <TeamManagement />
            </div>
          {/if}

          <!-- Admin System Settings: removed (retry config moved to task health) -->

          <!-- Embedding Migration Section -->
          {#if activeSection === 'embedding-migration'}
            <div class="content-section">
              <EmbeddingMigrationSettings />
              <EmbeddingConsistencySettings />
            </div>
          {/if}

          <!-- Data Integrity Section -->
          {#if activeSection === 'data-integrity'}
            <div class="content-section">
              <DataIntegritySettings />
            </div>
          {/if}

          <!-- File Retention Section (includes derived media cache) -->
          {#if activeSection === 'retention'}
            <div class="content-section">
              <RetentionSettings />
            </div>
          {/if}

          <!-- Scheduled Database Backups Section -->
          {#if activeSection === 'backup'}
            <div class="content-section">
              <BackupSettings />
            </div>
          {/if}

          <!-- Authentication Settings Section (Super Admin only) -->
          {#if activeSection === 'authentication'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.authentication.title')}</h3>
              <p class="section-description">{$t('settings.authentication.description')}</p>
              <AuthenticationSettings />
            </div>
          {/if}


          <!-- Audit Log Viewer (Super Admin only) -->
          {#if activeSection === 'audit-logs'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.auditLog.sectionTitle')}</h3>
              <p class="section-description">{$t('settings.auditLog.sectionDescription')}</p>
              <AuditLogViewer />
            </div>
          {/if}
          {/if}
        </main>
      </div>
    </div>
  </div>
{/if}

<!-- Close Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={showCloseConfirmation}
  title={$t('settings.unsavedChanges')}
  message={$t('settings.unsavedChangesMessage')}
  confirmText={$t('settings.closeWithoutSaving')}
  cancelText={$t('settings.keepEditing')}
  confirmButtonClass="btn-danger"
  cancelButtonClass="btn-secondary"
  on:confirm={forceClose}
  on:cancel={() => showCloseConfirmation = false}
  on:close={() => showCloseConfirmation = false}
/>

<!-- Admin Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={showConfirmModal}
  title={confirmModalTitle}
  message={confirmModalMessage}
  confirmText={$t('settings.confirm')}
  cancelText={$t('settings.cancel')}
  confirmButtonClass="btn-primary"
  cancelButtonClass="btn-secondary"
  on:confirm={handleConfirmModalConfirm}
  on:cancel={handleConfirmModalCancel}
  on:close={handleConfirmModalCancel}
/>

<!-- Processing Details Modal -->
<ProcessingDetailsModal
  bind:isOpen={showProcessingDetails}
  bind:section={processingDetailsSection}
  {stats}
/>

<style>
  .settings-modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: var(--modal-backdrop);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1300;
    animation: fadeIn 0.2s ease-out;
    overflow: hidden;
    overscroll-behavior: none;
  }

  .settings-modal {
    position: relative;
    width: 90vw;
    max-width: 1200px;
    height: 85vh;
    max-height: 900px;
    background-color: var(--surface-color);
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    overflow: hidden;
    animation: slideUp 0.3s ease-out;
    display: flex;
    flex-direction: column;
  }

  .settings-header-bar {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
    min-height: 44px;
  }

  .settings-header-title {
    font-weight: 600;
    font-size: 1.1rem;
    color: var(--text-color);
    margin: 0;
    margin-right: auto;
  }

  .modal-close-button {
    position: absolute;
    top: 0.75rem;
    right: 0.75rem;
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.5rem;
    color: var(--text-secondary);
    transition: color 0.2s ease;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10;
  }

  .modal-close-button:hover {
    color: var(--text-color);
    background: var(--button-hover, var(--background-color));
  }

  .settings-modal-container {
    display: flex;
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }

  .settings-sidebar {
    width: 240px;
    background-color: var(--background-color);
    border-right: 1px solid var(--border-color);
    padding: 0.75rem 0;
    overflow-y: auto;
    flex-shrink: 0;
    overscroll-behavior: contain;
  }

  .sidebar-section {
    margin-bottom: 0.25rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--border-color);
  }

  .sidebar-section:first-child {
    border-top: none;
    padding-top: 0;
  }

  .section-heading {
    font-size: 0.6875rem;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-tertiary, var(--text-secondary));
    opacity: 0.7;
    margin: 0 1.25rem 0.375rem;
    padding-top: 0;
  }

  .section-nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .nav-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.4375rem 0.75rem;
    margin: 0 0.5rem;
    border: none;
    border-radius: 6px;
    background-color: transparent;
    color: var(--text-color);
    text-align: left;
    cursor: pointer;
    transition: background-color 0.15s, color 0.15s;
    font-size: 0.8125rem;
    position: relative;
  }

  .nav-item:hover {
    background-color: var(--hover-color, rgba(0, 0, 0, 0.04));
    color: var(--primary-color);
  }

  .nav-item.active {
    background-color: var(--primary-light);
    color: var(--primary-color);
    font-weight: 500;
  }

  /* Present but not openable: the entry has to stay legible enough to read as a
     real feature (that is the whole point of not omitting it) while clearly not
     being actionable. Same token in both themes, so light/dark stay in parity. */
  .nav-item.locked {
    color: var(--text-secondary);
    opacity: 0.55;
    cursor: not-allowed;
  }

  .nav-item.locked:hover {
    background-color: transparent;
    color: var(--text-secondary);
  }

  .nav-item-label {
    flex: 1;
  }

  .lock-indicator {
    display: flex;
    align-items: center;
    color: var(--text-secondary);
    flex-shrink: 0;
  }

  .dirty-indicator {
    color: var(--warning-color);
    font-size: 1.2em;
    line-height: 1;
  }

  /* Pending-work count (currently the account-approval queue). Amber rather than
     the primary colour so it reads as "needs attention", and it uses explicit
     light/dark values because the amber tokens are not defined in both themes. */
  .nav-badge {
    flex-shrink: 0;
    min-width: 1.25rem;
    padding: 0 0.35rem;
    border-radius: 999px;
    background: rgba(245, 158, 11, 0.18);
    color: #b45309;
    border: 1px solid rgba(245, 158, 11, 0.45);
    font-size: 0.6875rem;
    font-weight: 700;
    line-height: 1.4;
    text-align: center;
  }

  :global([data-theme='dark']) .nav-badge {
    background: rgba(245, 158, 11, 0.22);
    color: #fbbf24;
  }

  .settings-content {
    flex: 1;
    overflow-y: auto;
    padding: 1.5rem;
    overscroll-behavior: contain;
  }

  /* Mobile section navigation - hidden when sidebar is visible */
  .mobile-section-nav {
    display: none;
    margin-bottom: 1rem;
    padding: 0.5rem;
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  .mobile-nav-label {
    display: block;
    font-size: 0.75rem;
    font-weight: 600;
    margin-bottom: 0.25rem;
    color: var(--text-secondary);
  }

  .mobile-nav-select {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 1rem;
    min-height: 44px;
  }

  .content-section {
    max-width: 100%;
  }

  .section-title {
    font-size: 1.125rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
    color: var(--text-color);
  }

  .section-description {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin: 0 0 1.25rem 0;
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .form-group label {
    font-weight: 500;
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  .form-control {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    transition: border-color 0.15s, box-shadow 0.15s;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px var(--primary-light);
  }

  .form-control:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    background-color: var(--background-color);
  }

  .form-text {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-top: 0.125rem;
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-weight: normal;
    font-size: 0.8125rem;
  }

  .checkbox-label input[type="checkbox"] {
    width: 16px;
    height: 16px;
    cursor: pointer;
  }

  .form-actions {
    display: flex;
    gap: 0.75rem;
    margin-top: 0.75rem;
    justify-content: flex-end;
  }

  .form-actions .btn-secondary {
    margin-right: auto;
  }

  .btn {
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: none;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-primary {
    background-color: #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-primary:hover:not(:disabled) {
    background-color: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-primary:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-secondary {
    background-color: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
  }

  .btn-secondary:hover:not(:disabled) {
    background-color: var(--button-hover);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
  }

  .btn-secondary:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-secondary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Responsive Design */

  /* Raise above navbar (z-index 1200) on tablet so close button is reachable */
  @media (max-width: 768px) {
    .settings-modal {
      width: 100vw;
      height: 100vh;
      height: 100dvh;
      max-width: none;
      max-height: none;
      border-radius: 0;
      padding-top: env(safe-area-inset-top, 0px);
      padding-bottom: env(safe-area-inset-bottom, 0px);
    }

    .settings-modal-container {
      flex-direction: column;
      overflow: visible;
    }

    /* Hide full sidebar on mobile — use select dropdown */
    .settings-sidebar {
      display: none;
    }

    .mobile-section-nav {
      display: block;
    }

    .settings-header-bar {
      padding: 0.75rem 1rem;
    }

    .settings-content {
      padding: 1rem;
      flex: 1 1 0;
      min-height: 0;
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
      overflow-x: hidden;
    }

    /* Global mobile overrides for all settings panels */
    .settings-content :global(.form-group) {
      min-width: 0;
    }

    .settings-content :global(input),
    .settings-content :global(select),
    .settings-content :global(textarea) {
      min-height: 44px;
      font-size: 1rem;
      max-width: 100%;
      box-sizing: border-box;
    }

    .settings-content :global(button) {
      min-height: 44px;
    }

    .form-actions {
      flex-direction: column-reverse;
    }

    .form-actions .btn {
      width: 100%;
      min-height: 44px;
      text-align: center;
    }

    .form-actions .btn-secondary {
      margin-right: 0;
    }
  }
</style>
