<script lang="ts">
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { onMount, onDestroy, tick } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import InfoTip from '$components/settings/transcription/InfoTip.svelte';
  import { page } from '$app/state';
  import { afterNavigate } from '$app/navigation';
  import { reopenSettingsAfterBack } from '$lib/settings/returnToSettings';
  import { user as userStore } from '$stores/auth';
  import { capabilities } from '$stores/capabilities';
  import { isCloudEdition } from '$lib/edition';
  import { settingsModalStore, type SettingsSection } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import { listLegalDocuments } from '$lib/api/legal';
  import type { RecordingSettings } from '$lib/api/userSettings';

  // Sections are lazy (LazySection + sectionLoaders): only the pieces the modal itself needs
  // are imported statically.
  import SettingsModalFrame from '$components/settings/SettingsModalFrame.svelte';
  import LazySection from '$components/settings/LazySection.svelte';
  import {
    sectionLoaders,
    startIdlePrefetch,
    prefetchSectionIntent,
    type PrefetchRole,
  } from '$lib/settings/sectionLoaders';
  import { invalidateSectionData, SectionDataKey } from '$lib/settings/sectionData';
  import type { ConfirmRequest } from '$components/settings/AdminTaskHealthPanel.svelte';
  import { TakedownNoticesApi } from '$lib/api/takedownNotices';
  import { SUPPORT_ACCESS_EVENT } from '$lib/supportAccess/events';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import ProcessingDetailsModal from '$components/settings/ProcessingDetailsModal.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';

  import SettingsSearch from '$components/settings/SettingsSearch.svelte';
  import type { SettingsSearchItem } from '$lib/search/settingsSearchIndex';
  import type { FuzzyIndex } from '$lib/search/fuzzyMatcher';
  import { SECTION_MIN_ROLE, sectionLocked, lockReason as lockReasonFor } from '$lib/settings/sectionAccess';
  import {
    buildSidebarSections,
    capOn,
    orgAdminCapOn as orgAdminCapOnFor,
    orgPrivacyOn,
  } from '$lib/settings/sidebarSections';
  import {
    buildSettingsFuzzyIndex,
    buildVisibleSections,
    flashSettingControl as flashControl,
  } from '$lib/settings/settingsSearch';
  import { buildSectionAccess } from '$lib/settings/sectionCapabilities';
  import { loadRecording, saveRecording, resetRecording } from '$lib/settings/recordingSettings';
  import {
    countPendingApprovals,
    countSupportRequests,
    fetchAdminUsers,
    fetchSystemStats,
    initialSystemStats,
    recoverUserFilesRequest,
    type AdminUserRow,
    type SearchHealthStatus,
    type SearchIndexStatus,
  } from '$lib/settings/adminLoaders';
  import {
    aliasIdsOf,
    dataTabFor,
    statusTabFor,
    sidebarRowFor,
    speakerIdTabFor,
    transcriptionTabFor,
    watchSourcesTabFor,
  } from '$lib/settings/sectionAliases';

  // Import i18n
  import { t, locale } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

  /** The root layout shows the shell first when the chunk was still loading; skip the enter animation then. */
  export let skipEnter = false;

  // Modal state
  let modalElement: HTMLElement | undefined;
  // The nav list (not the sidebar) scrolls; the search header above it draws a divider once it has.
  let sidebarScrolled = false;
  let showCloseConfirmation = false;

  // Settings state
  $: isOpen = $settingsModalStore.isOpen;
  $: activeSection = $settingsModalStore.activeSection;

  // Settings search (macOS-style). Index is rebuilt when the visible-section set
  // (capabilities/edition) or the locale changes; desktop + mobile instances share it.
  let desktopSearchActive = false;
  let mobileSearchActive = false;
  let settingsSearchIndex: FuzzyIndex<SettingsSearchItem> | null = null;

  // Browser Back onto a page this modal was open over (it closes itself on every route change)
  // reopens it on the section it was left on. Here, not in the root layout: the layout's static
  // size is budgeted, and the modal is mounted whenever a link could have stamped a history entry.
  afterNavigate(({ type }) => {
    if (type === 'popstate') reopenSettingsAfterBack();
  });

  // Close modal when the user navigates to a different page
  let _prevPath = '';
  $: {
    const newPath = page.url.pathname;
    if (isOpen && _prevPath && newPath !== _prevPath) {
      closeModal();
    }
    _prevPath = newPath;
  }
  $: isAdmin = $userStore?.role === 'admin' || $userStore?.role === 'super_admin';
  $: isSuperAdmin = $userStore?.role === 'super_admin';

  // An alias id is judged by the row that now holds it (retention -> Data & Storage).
  $: activeSectionLocked = sectionLocked(sidebarRowFor(activeSection), isAdmin, isSuperAdmin);
  $: activeSectionRequires = SECTION_MIN_ROLE[sidebarRowFor(activeSection)];

  const lockReason = (section: SettingsSection): string => lockReasonFor(section, $t);

  // Recording settings section
  let maxRecordingDuration = 120;
  let recordingQuality: 'standard' | 'high' | 'maximum' = 'high';
  let autoStopEnabled = true;
  let recordingSettingsChanged = false;
  let recordingSettingsLoading = false;

  // Admin Users section
  let users: AdminUserRow[] = [];
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
    pendingApprovalCount = await countPendingApprovals();
  }

  /**
   * Support-access requests awaiting this user's decision (issue #1122), across their personal
   * workspace and, for an organization admin, their organization. Owned here, like the
   * approval count, because the sidebar badge shows while another section is open.
   */
  let supportRequestCount = 0;
  $: supportOrgTab = orgAdminCapOn(capState, 'organizations') && $userStore?.org_role === 'org:admin';

  async function loadSupportRequestCount() {
    supportRequestCount = await countSupportRequests(tenancyMulti, supportOrgTab);
  }

  // Admin Stats section
  let stats = initialSystemStats($t);
  let showProcessingDetails = false;
  let processingDetailsSection = 'performance';
  let statsLoading = false;
  let statsRefreshing = false;
  let statsInitialLoaded = false;
  let gpuRetryScheduled = false;

  // Search index status (for system stats card)
  let searchIndexStatus: SearchIndexStatus | null = null;
  let searchHealthStatus: SearchHealthStatus | null = null;

  // Admin Task Health confirmation flow (panel dispatches, modal owns ConfirmationModal)
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
  } = confirmDialog;

  // Edition capability gating (server-driven entitlements): a section item
  // tagged with `cap` renders only when the backend lists that capability as
  // enabled for this deployment. Community = everything on (no change);
  // cloud hides platform/self-host surfaces so the product "just works".
  // The backend independently 404s gated endpoints — this is cosmetic only.
  $: capState = $capabilities;
  // Support-access UI exists only in multi-tenant mode and is FAIL-CLOSED (unknown = hidden),
  // unlike `capOn`, which is fail-open. Do not route this through isCapabilityEnabled.
  $: tenancyMulti = capState.tenancyMode === 'multi';

  $: access = buildSectionAccess(capState, isAdmin, isSuperAdmin);
  $: transcriptionAccess = access.transcription;
  $: speakerIdAccess = access.speakerId;
  $: dataAccess = access.data;
  $: statusAccess = access.status;
  $: privacyAccess = access.privacy;

  // The modal's default landing section is 'system-statistics' (self-host: a
  // reasonable dashboard for any signed-in user). The capabilities store is
  // fail-open until its fetch resolves (see stores/capabilities.ts) so on a
  // deployment where system.hardware_stats is disabled (cloud), the default
  // section is briefly selected before capabilities arrive and hide its nav
  // entry. A purely local derived fallback (rather than writing the correction
  // back into settingsModalStore) — the render condition below reads this
  // instead of `activeSection` directly, so there's no cross-store update to
  // land in the same reactive flush as the capabilities change. No-op for
  // self-host, where the capability is always enabled and this never departs
  // from `activeSection`.
  $: effectiveActiveSection =
    activeSection === 'system-statistics' && capState.loaded && !capOn(capState, 'system.hardware_stats')
      ? 'profile'
      : sidebarRowFor(activeSection); // aliases highlight the row that now holds them

  const orgAdminCapOn = orgAdminCapOnFor;

  $: sidebarSections = buildSidebarSections({
    t: $t,
    capState,
    isAdmin,
    isSuperAdmin,
    orgRole: $userStore?.org_role,
    tenancyMulti,
    supportRequestCount,
    pendingApprovalCount,
    hasTakedowns,
    legalAvailable,
    transcriptionAccess,
    speakerIdAccess,
    dataAccess,
    privacyAccess,
  });

  $: visibleSections = buildVisibleSections($t, sidebarSections, {
    isAdmin,
    isSuperAdmin,
    transcriptionAccess,
    speakerIdAccess,
    dataAccess,
    statusAccess,
    privacyAccess,
  });

  const indexFor = (_locale: string, sections: Parameters<typeof buildSettingsFuzzyIndex>[0]) =>
    buildSettingsFuzzyIndex(sections);
  // Rebuild the fuzzy index only while the modal is open. `$locale` is referenced
  // so the index refreshes when the UI language changes (labels are localized).
  $: settingsSearchIndex = isOpen ? indexFor($locale, visibleSections) : null;

  // Any change of section (sidebar click, alias, search hit, in-panel jump link) must show where the
  // user landed: a row far below the fold is otherwise invisible and the change reads as "nothing
  // happened". One reactive hook covers every path instead of each caller scrolling.
  // The list is unmounted while a search is open, so it also re-reveals when the search closes.
  $: void revealActiveNavItem(effectiveActiveSection, desktopSearchActive);
  $: if (desktopSearchActive) sidebarScrolled = false;

  /** A row is dirty when it, or any legacy id that now opens one of its tabs, has unsaved edits. */
  function rowDirty(dirty: Record<string, boolean>, row: SettingsSection): boolean {
    return !!dirty[row] || aliasIdsOf(row).some((id) => !!dirty[id]);
  }

  async function revealActiveNavItem(_section: string, _searching: boolean) {
    await tick();
    const row = modalElement?.querySelector('.settings-sidebar .nav-item.active');
    if (!row?.scrollIntoView) return;
    const calm = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    row.scrollIntoView({ block: 'nearest', behavior: calm ? 'auto' : 'smooth' });
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

  const flashSettingControl = (anchorText: string) => flashControl(modalElement, anchorText);

  // Reactive recording settings change detection
  $: {
    settingsModalStore.setDirty('recording', recordingSettingsChanged);
  }

  let legalAvailable = false;
  let hasTakedowns = false;

  onMount(() => {
    listLegalDocuments()
      .then((list) => (legalAvailable = list.length > 0))
      .catch(() => (legalAvailable = false));

    TakedownNoticesApi.listMine({ limit: 1 })
      .then((page) => (hasTakedowns = page.total > 0))
      .catch(() => (hasTakedowns = false));

    // Load recording settings
    loadRecordingSettings();

    // Section data and the admin badge count are NOT loaded here: this component mounts already
    // open (the layout imports it on demand), so the "just opened" block below runs on init and
    // owns those loads. Loading them in both places requested every endpoint twice.

    // Add escape key listener
    document.addEventListener('keydown', handleKeyDown);
    window.addEventListener('gpu-stats-updated', handleGpuStatsEvent);
    window.addEventListener('reindex-complete', handleReindexCompleteStats);
    window.addEventListener(SUPPORT_ACCESS_EVENT, loadSupportRequestCount);
  });

  onDestroy(() => {
    document.removeEventListener('keydown', handleKeyDown);
    window.removeEventListener('gpu-stats-updated', handleGpuStatsEvent);
    window.removeEventListener('reindex-complete', handleReindexCompleteStats);
    window.removeEventListener(SUPPORT_ACCESS_EVENT, loadSupportRequestCount);
    cancelWarmup?.();
    if (previousOpenState) unlockScroll();
  });

  // Background warm-up of the other sections (code on idle, a few cheap reads), for the sections
  // this user can open only. Restarts when the visible set changes; cancelled on close/destroy.
  $: prefetchRole = (isSuperAdmin ? 'super_admin' : isAdmin ? 'admin' : 'user') as PrefetchRole;
  $: warmKey = isOpen
    ? `${prefetchRole}|${sidebarSections
        .flatMap((group) => group.items)
        .filter((item) => !item.locked)
        .map((item) => item.id)
        .join(',')}`
    : '';
  let appliedWarmKey = '';
  let cancelWarmup: (() => void) | null = null;
  $: syncWarmup(warmKey);

  function syncWarmup(key: string) {
    if (key === appliedWarmKey) return;
    appliedWarmKey = key;
    cancelWarmup?.();
    cancelWarmup = null;
    if (!key) return;
    const ids = key.slice(key.indexOf('|') + 1).split(',') as SettingsSection[];
    const role = prefetchRole;
    // Let the section the user opened on paint first.
    const timer = window.setTimeout(() => {
      cancelWarmup = startIdlePrefetch(ids, role, activeSection);
    }, 400);
    cancelWarmup = () => window.clearTimeout(timer);
  }

  // The pipeline overview is derived from other sections' settings; a save elsewhere makes the
  // remembered copy wrong, so drop it (the overview still refreshes on every visit).
  let hadDirty = false;
  $: {
    const anyDirty = settingsModalStore.hasAnyDirty($settingsModalStore);
    if (hadDirty && !anyDirty) invalidateSectionData(SectionDataKey.pipeline);
    hadDirty = anyDirty;
  }

  // Track previous open state to detect when modal opens
  let previousOpenState = false;

  // Prevent body scroll when modal is open and load initial data
  $: {
    if (isOpen && !previousOpenState) {
      // Modal just opened — prevent background scroll
      lockScroll();

      // Load data for the active section when modal opens.
      // The file-status tabs self-load on their panel's mount.
      if (activeSection === 'system-statistics') {
        loadStats();
      } else if (activeSection === 'admin-users' && isAdmin) {
        loadAdminUsers();
      }
      if (isAdmin) loadPendingApprovalCount();
      void loadSupportRequestCount();

      previousOpenState = true;
    } else if (!isOpen && previousOpenState) {
      // Modal just closed — restore background scroll
      unlockScroll();
      previousOpenState = false;
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === 'Escape' && isOpen) {
      // A nested BaseModal (support-access request, break-glass, approve, access log...) owns
      // this Escape: its own handler closes it. Closing the whole settings dialog as well threw
      // away an in-progress break-glass confirmation along with it.
      if (document.querySelector('.modal-backdrop')) return;
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
    // The Recovery tab of file-status self-loads on its panel's mount.
  }

  // Recording settings functions
  async function loadRecordingSettings() {
    recordingSettingsLoading = true;
    const settings = await loadRecording($t);
    if (settings) {
      maxRecordingDuration = settings.max_recording_duration;
      recordingQuality = settings.recording_quality;
      autoStopEnabled = settings.auto_stop_enabled;
      recordingSettingsChanged = false;
    }
    recordingSettingsLoading = false;
  }

  function handleRecordingSettingsChange() {
    recordingSettingsChanged = true;
    settingsModalStore.setDirty('recording', true);
  }

  async function saveRecordingSettings() {
    recordingSettingsLoading = true;
    const settings: RecordingSettings = {
      max_recording_duration: maxRecordingDuration,
      recording_quality: recordingQuality,
      auto_stop_enabled: autoStopEnabled
    };
    if (await saveRecording(settings, $t)) {
      recordingSettingsChanged = false;
      settingsModalStore.clearDirty('recording');
    }
    recordingSettingsLoading = false;
  }

  async function resetRecordingSettings() {
    recordingSettingsLoading = true;
    if (await resetRecording($t, loadRecordingSettings)) {
      recordingSettingsChanged = false;
      settingsModalStore.clearDirty('recording');
    }
    recordingSettingsLoading = false;
  }

  // Admin functions
  async function loadAdminUsers(showLoading = true) {
    // Only show loading spinner on initial load, not on refresh
    if (showLoading) {
      usersLoading = true;
    }

    try {
      users = await fetchAdminUsers();
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
      await recoverUserFilesRequest(userId);
      toastStore.success($t('settings.toast.userRecoveryInitiated'));
    } catch (err: unknown) {
      console.error('Error recovering user files:', err);
      const message = getErrorMessage(err, $t('settings.toast.userRecoveryFailed'));
      toastStore.error(message);
    }
  }

  async function loadStats() {
    // Cloud only: capabilities are fail-open until their fetch resolves (see
    // stores/capabilities.ts), and system-statistics is the modal's default
    // section, so on mount this can fire before capabilities confirm
    // system.hardware_stats is disabled for this tenant -- hitting the
    // capability-gated endpoint and surfacing a user-visible 404 toast for a
    // panel the user never asked to see. isCloudEdition is a build-time
    // constant (not a fetch), so this adds no delay/flicker for self-host,
    // where the capability is always enabled and this condition never holds.
    if (isCloudEdition && (!capState.loaded || !capOn(capState, 'system.hardware_stats'))) {
      return;
    }
    if (statsInitialLoaded) {
      statsRefreshing = true;
    } else {
      statsLoading = true;
    }

    try {
      const result = await fetchSystemStats();

      stats = result.stats;
      statsInitialLoaded = true;

      if (result.indexStatus) searchIndexStatus = result.indexStatus;
      if (result.indexHealth) searchHealthStatus = result.indexHealth;

      // Auto-retry once if GPU stats are loading
      if (result.stats?.system?.gpus?.[0]?.loading && !gpuRetryScheduled) {
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

  function handleTaskHealthConfirm(event: CustomEvent<ConfirmRequest>) {
    const { title, message, callback } = event.detail;
    confirmDialog.request(title, message, callback);
  }

  // AI settings change handlers
  function onAISettingsChange() {
    // Handler for AI settings changes - can be extended for additional logic
  }
</script>

{#if isOpen}
  <SettingsModalFrame bind:element={modalElement} {skipEnter} on:close={attemptClose}>
    <svelte:fragment slot="sidebar">
      <!-- Desktop sidebar -->
      <SettingsSearch
        index={settingsSearchIndex}
        idPrefix="settings-search-desktop"
        layout="sidebar"
        raised={sidebarScrolled}
        bind:active={desktopSearchActive}
        on:navigate={handleSettingsNavigate}
      />
      {#if !desktopSearchActive}
        <div
          class="sidebar-scroll"
          on:scroll={(event) => (sidebarScrolled = event.currentTarget.scrollTop > 0)}
        >
        {#each sidebarSections as section}
          <div class="sidebar-section">
            <h3 class="section-heading">{section.title}</h3>
            <nav class="section-nav">
              {#each section.items as item}
                <button
                  class="nav-item"
                  class:active={effectiveActiveSection === item.id}
                  class:dirty={rowDirty($settingsModalStore.dirtyState, item.id)}
                  class:locked={item.locked}
                  disabled={item.locked}
                  title={item.locked ? lockReason(item.id) : undefined}
                  on:click={() => switchSection(item.id)}
                  on:mouseenter={() => !item.locked && prefetchSectionIntent(item.id, prefetchRole)}
                  on:focus={() => !item.locked && prefetchSectionIntent(item.id, prefetchRole)}
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
                  {:else if rowDirty($settingsModalStore.dirtyState, item.id)}
                    <span class="dirty-indicator" role="img" aria-label={$t('settings.unsavedChanges')} title={$t('settings.unsavedChanges')}><MenuIcon name="dot" size={10} /></span>
                  {/if}
                </button>
              {/each}
            </nav>
          </div>
        {/each}
        </div>
      {/if}
    </svelte:fragment>

    <!-- Content Area -->
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
          value={effectiveActiveSection}
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
          icon="lock"
          title={activeSectionRequires === 'super_admin'
            ? $t('settings.permission.superAdminTitle')
            : $t('settings.permission.adminTitle')}
          description={activeSectionRequires === 'super_admin'
            ? $t('settings.permission.superAdminMessage')
            : $t('settings.permission.adminMessage')}
        />
      </div>
    {:else}
    <!-- Every section's heading renders at once; its panel loads on first use (LazySection),
         so a section's chunk is only fetched when somebody opens it. -->

    <!-- Profile Section -->
    {#if effectiveActiveSection === 'profile'}
      <LazySection load={sectionLoaders.profile} let:Component>
        <Component />
      </LazySection>
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
        <LazySection load={sectionLoaders.audioExtraction} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Processing pipeline: read-only view of how the next recording is processed. Mounted
         only while open, so every visit reads the current plan. -->
    {#if activeSection === 'processing-pipeline'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.pipeline.title')}</h3>
        <p class="section-description">{$t('settings.pipeline.description')}</p>
        <LazySection load={sectionLoaders.pipeline} let:Component>
          <Component on:navigate={(e: CustomEvent<SettingsSection>) => switchSection(e.detail)} />
        </LazySection>
      </div>
    {/if}

    <!-- Transcription: language, provider and model, vocabulary, accuracy (tabs).
         asr-provider and custom-vocabulary are kept as ids and open their tab. -->
    {#if sidebarRowFor(activeSection) === 'transcription'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.transcription.title')}</h3>
        <p class="section-description">{$t('settings.transcription.description')}</p>
        <LazySection load={sectionLoaders.transcription} let:Component>
          <Component
            {isAdmin}
            {isSuperAdmin}
            {...transcriptionAccess}
            initialTab={transcriptionTabFor(activeSection) ?? 'tx-language'}
          />
        </LazySection>
      </div>
    {/if}

    <!-- Speaker Identification: detection, voice attributes, engine, maintenance (tabs).
         speaker-attributes and engine-settings are kept as ids and open their tab. -->
    {#if sidebarRowFor(activeSection) === 'speaker-identification'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.speakerIdentification.title')}</h3>
        <p class="section-description">{$t('settings.speakerIdentification.description')}</p>
        <LazySection load={sectionLoaders.speakerIdentification} let:Component>
          <Component
            {...speakerIdAccess}
            embeddingCap={capOn(capState, 'admin.embedding_migration')}
            initialTab={speakerIdTabFor(activeSection) ?? 'spk-detection'}
            on:navigate={(e: CustomEvent<SettingsSection>) => switchSection(e.detail)}
          />
        </LazySection>
      </div>
    {/if}

    <!-- Organization Context Section -->
    {#if activeSection === 'organization-context'}
      <div class="content-section">
        <div class="section-title-row">
          <h3 class="section-title">{$t('settings.orgContext.title')}</h3>
          <InfoTip label={$t('settings.orgContext.howItWorks')}>
            <strong>{$t('settings.orgContext.howItWorks')}</strong>
            <ul>
              <li>{$t('settings.orgContext.howItWorks1')}</li>
              <li>{$t('settings.orgContext.howItWorks2')}</li>
              <li>{$t('settings.orgContext.howItWorks3')}</li>
              <li>{$t('settings.orgContext.howItWorks4')}</li>
            </ul>
          </InfoTip>
        </div>
        <p class="section-description">{$t('settings.orgContext.description')}</p>
        <LazySection load={sectionLoaders.organizationContext} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Download Settings Section -->
    {#if activeSection === 'download'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.download.title')}</h3>
        <p class="section-description">{$t('settings.download.description')}</p>
        <LazySection load={sectionLoaders.download} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Media Sources Settings Section -->
    {#if activeSection === 'media-sources'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.mediaSources.title')}</h3>
        <p class="section-description">{$t('settings.mediaSources.description')}</p>
        <LazySection load={sectionLoaders.mediaSources} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Watch Sources Settings Section -->
    {#if sidebarRowFor(activeSection) === 'watch-sources'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.watchSources.title')}</h3>
        <p class="section-description">{$t('settings.watchSources.description')}</p>
        <LazySection load={sectionLoaders.watchSources} let:Component>
          <Component initialTab={watchSourcesTabFor(activeSection) ?? 'sources'} />
        </LazySection>
      </div>
    {/if}

    <!-- AI Prompts Section -->
    {#if activeSection === 'ai-prompts'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.aiPrompts.title')}</h3>
        <p class="section-description">{$t('settings.aiPrompts.description')}</p>
        <LazySection load={sectionLoaders.prompts} let:Component>
          <Component onSettingsChange={onAISettingsChange} />
        </LazySection>
      </div>
    {/if}

    <!-- LLM Provider Section -->
    {#if activeSection === 'llm-provider'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.llmProvider.title')}</h3>
        <p class="section-description">{$t('settings.llmProvider.description')}</p>
        <LazySection load={sectionLoaders.llm} let:Component>
          <Component onSettingsChange={onAISettingsChange} {isAdmin} />
        </LazySection>
      </div>
    {/if}

    <!-- AI usage: tokens against the limits (issue #790) -->
    {#if activeSection === 'llm-usage'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.llmUsage.title')}</h3>
        <p class="section-description">{$t('settings.llmUsage.description')}</p>
        <LazySection load={sectionLoaders.llmUsage} let:Component>
          <Component {isAdmin} />
        </LazySection>
      </div>
    {/if}

    <!-- Auto-Labeling Section -->
    {#if activeSection === 'auto-labeling'}
      <div class="content-section">
        <h3 class="section-title">{$t('autoLabel.title')}</h3>
        <p class="section-description">{$t('autoLabel.description')}</p>
        <LazySection load={sectionLoaders.autoLabel} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Privacy & Redaction: per-user preference tab + admin policy tab.
         'redaction-policy' is kept as a section id (settings search, deep links)
         and opens the policy tab of this same panel. -->
    {#if activeSection === 'content-redaction' || activeSection === 'redaction-policy'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.privacyRedaction.title')}</h3>
        <LazySection load={sectionLoaders.privacyRedaction} let:Component>
          <Component
            {isAdmin}
            {isSuperAdmin}
            userCap={privacyAccess.userCap}
            policyCap={privacyAccess.policyCap}
            initialTab={activeSection === 'redaction-policy' ? 'policy' : 'personal'}
          />
        </LazySection>
      </div>
    {/if}

    <!-- Chat: per-user defaults + (admin) platform tuning, as tabs.
         'chat-admin' is kept as a section id so the settings search and any
         existing deep link still resolve — it just opens the Advanced tab
         of the same panel instead of a second sidebar entry. -->
    {#if activeSection === 'chat' || activeSection === 'chat-admin'}
      <div class="content-section">
        <h3 class="section-title">{$t('chat.settings.title')}</h3>
        <LazySection load={sectionLoaders.chat} let:Component>
          <Component
            {isAdmin}
            initialTab={activeSection === 'chat-admin' && isAdmin ? 'chat-advanced' : 'chat-general'}
          />
        </LazySection>
      </div>
    {/if}

    <!-- Search & Indexing Section -->
    <!-- Groups Section -->
    {#if activeSection === 'groups'}
      <div class="content-section">
        <LazySection load={sectionLoaders.groups} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Admin Users Section -->
    {#if activeSection === 'admin-users'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.users.title')}</h3>
        <p class="section-description">{$t('settings.users.description')}</p>
        {#if isSuperAdmin}
          <LazySection load={sectionLoaders.accountStatus} lines={2} let:Component>
            <Component />
          </LazySection>
        {/if}
        <!-- Above the table on purpose: a queue is work waiting, and the
             accounts in it are not in the table's list until they are
             admitted. `onDecided` refreshes the user list so an approved
             account appears there in the same interaction. -->
        <LazySection load={sectionLoaders.pendingApprovals} lines={2} let:Component>
          <Component
            onDecided={refreshAdminUsers}
            on:countchange={(e: CustomEvent<number>) => (pendingApprovalCount = e.detail)}
          />
        </LazySection>
        <LazySection load={sectionLoaders.userTable} let:Component>
          <Component
            {users}
            loading={usersLoading}
            onRefresh={refreshAdminUsers}
            onUserRecovery={recoverUserFiles}
          />
        </LazySection>
        <LazySection load={sectionLoaders.lockedAccounts} lines={2} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Quarantine / Takedown Review Queue Section (issue #576) -->
    {#if activeSection === 'quarantine'}
      <div class="content-section">
        <LazySection load={sectionLoaders.quarantine} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Operator legal holds, no takedown (legal-hold plan §8.4); release lives here only -->
    {#if activeSection === 'platform-holds'}
      <div class="content-section">
        <h3 class="section-title">{$t('holds.platform.title')}</h3>
        <LazySection load={sectionLoaders.legalHolds} let:Component>
          <Component scope="platform" />
        </LazySection>
      </div>
    {/if}

    <!-- Takedown notice records (issue #1254) -->
    {#if activeSection === 'takedown-notices'}
      <div class="content-section">
        <LazySection load={sectionLoaders.takedownNotices} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    {#if activeSection === 'my-takedowns' && hasTakedowns}
      <div class="content-section">
        <LazySection load={sectionLoaders.myTakedowns} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Support access requests (issue #1122): every user decides for their own workspace -->
    {#if activeSection === 'support-access-requests' && tenancyMulti}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.supportAccessRequests.title')}</h3>
        <LazySection load={sectionLoaders.supportApprovals} let:Component>
          <Component orgTab={supportOrgTab} on:countchange={loadSupportRequestCount} />
        </LazySection>
      </div>
    {/if}

    <!-- Support access (issue #1122): multi-tenant deployments only -->
    {#if activeSection === 'support-access' && tenancyMulti}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.supportAccess.title')}</h3>
        <LazySection load={sectionLoaders.supportStaff} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Legal documents -->
    {#if activeSection === 'legal' && legalAvailable}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.legal.title')}</h3>
        <LazySection load={sectionLoaders.legal} let:Component>
          <Component {isAdmin} {isSuperAdmin} isOrgAdmin={$userStore?.org_role === 'org:admin'} />
        </LazySection>
      </div>
    {/if}

    <!-- System Statistics Section -->
    {#if effectiveActiveSection === 'system-statistics'}
      <LazySection load={sectionLoaders.statistics} let:Component>
        <Component
          {stats}
          {statsLoading}
          {statsRefreshing}
          {searchIndexStatus}
          {searchHealthStatus}
          on:refresh={refreshStats}
          on:openDetails={(e: CustomEvent<string>) => openProcessingDetails(e.detail)}
        />
      </LazySection>
    {/if}

    <!-- File Status (condensed; the full page is /file-status) -->
    <!-- Tabs: Status (everyone) and Recovery (admins; the old admin-task-health id opens it). -->
    {#if sidebarRowFor(activeSection) === 'file-status'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.fileStatus.title')}</h3>
        <LazySection load={sectionLoaders.fileStatusSection} let:Component>
          <Component
            {...statusAccess}
            initialTab={statusTabFor(activeSection) ?? 'status-files'}
            on:requestConfirm={handleTaskHealthConfirm}
          />
        </LazySection>
      </div>
    {/if}

    <!-- Cloud edition — Billing (org admins only) -->
    {#if activeSection === 'billing' && orgAdminCapOn(capState, 'billing')}
      <div class="content-section">
        <h3 class="section-title">{$t('billing.title')}</h3>
        <p class="section-description">{$t('billing.description')}</p>
        <LazySection load={sectionLoaders.billing} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Cloud edition — Usage Dashboard (org admins only) -->
    {#if activeSection === 'usage' && orgAdminCapOn(capState, 'usage_dashboard')}
      <div class="content-section">
        <h3 class="section-title">{$t('usage.title')}</h3>
        <p class="section-description">{$t('usage.description')}</p>
        <LazySection load={sectionLoaders.usage} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Cloud edition — Team Management (org admins only) -->
    {#if activeSection === 'team' && orgAdminCapOn(capState, 'organizations')}
      <div class="content-section">
        <h3 class="section-title">{$t('team.title')}</h3>
        <p class="section-description">{$t('team.description')}</p>
        <LazySection load={sectionLoaders.team} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Cloud edition — organization takedown queue (org admins only, issue #1253) -->
    {#if activeSection === 'org-takedown' && orgAdminCapOn(capState, 'org.takedown')}
      <div class="content-section">
        <LazySection load={sectionLoaders.orgTakedown} let:Component>
          <Component scope="organization" />
        </LazySection>
      </div>
    {/if}

    <!-- Cloud edition — organization audit log (org admins only, issue #1277) -->
    {#if activeSection === 'org-audit-logs' && orgAdminCapOn(capState, 'org.audit_logs')}
      <div class="content-section">
        <h3 class="section-title">{$t('orgAudit.title')}</h3>
        <LazySection load={sectionLoaders.orgAuditLog} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Cloud edition — scheduled organization/member erasures (org admins only, issue #1105) -->
    {#if activeSection === 'org-erasures' && orgAdminCapOn(capState, 'organizations')}
      <div class="content-section">
        <h3 class="section-title">{$t('orgErasures.title')}</h3>
        <LazySection load={sectionLoaders.orgErasures} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Cloud edition — organization legal holds (org admins only, issue #1276) -->
    {#if activeSection === 'org-holds' && orgAdminCapOn(capState, 'org.legal_hold')}
      <div class="content-section">
        <h3 class="section-title">{$t('holds.title')}</h3>
        <LazySection load={sectionLoaders.legalHolds} let:Component>
          <Component scope="organization" />
        </LazySection>
      </div>
    {/if}

    <!-- Cloud edition — organization privacy policy (org admins only, issue #1280) -->
    {#if activeSection === 'org-privacy' && orgPrivacyOn(capState, $userStore?.org_role)}
      <div class="content-section">
        <h3 class="section-title">{$t('privacy.org.title')}</h3>
        <LazySection load={sectionLoaders.orgPrivacy} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Admin System Settings: removed (retry config moved to task health) -->

    <!-- Data & Storage: retention, media cache, backups, integrity, search index, speaker
         embeddings (tabs). retention / backup / data-integrity / search-indexing /
         embedding-migration are kept as ids and open their tab. -->
    {#if sidebarRowFor(activeSection) === 'data-management'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.dataManagement.title')}</h3>
        <p class="section-description">{$t('settings.dataManagement.description')}</p>
        <LazySection load={sectionLoaders.dataManagement} let:Component>
          <Component {...dataAccess} initialTab={dataTabFor(activeSection) ?? 'data-retention'} />
        </LazySection>
      </div>
    {/if}

    <!-- Authentication Settings Section (Super Admin only) -->
    {#if activeSection === 'authentication'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.authentication.title')}</h3>
        <p class="section-description">{$t('settings.authentication.description')}</p>
        <LazySection load={sectionLoaders.authentication} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}

    <!-- Audit Log Viewer (Super Admin only) -->
    {#if activeSection === 'audit-logs'}
      <div class="content-section">
        <h3 class="section-title">{$t('settings.auditLog.sectionTitle')}</h3>
        <p class="section-description">{$t('settings.auditLog.sectionDescription')}</p>
        <LazySection load={sectionLoaders.auditLog} let:Component>
          <Component />
        </LazySection>
      </div>
    {/if}
    {/if}
  </SettingsModalFrame>
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
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$t('settings.confirm')}
  cancelText={$t('settings.cancel')}
  confirmButtonClass="btn-primary"
  cancelButtonClass="btn-secondary"
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<!-- Processing Details Modal -->
<ProcessingDetailsModal
  bind:isOpen={showProcessingDetails}
  bind:section={processingDetailsSection}
  {stats}
/>


<style>
  .sidebar-scroll {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding-bottom: 0.75rem;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
  }

  .sidebar-section {
    margin-bottom: 0.25rem;
    padding-top: 0.75rem;
  }

  /* The divider belongs BETWEEN groups. Keep the sibling combinator: the top group must not
     draw a rule directly under the search header (it once did, when the search shared the
     scroller with the groups and `:first-child` could never match). */
  .sidebar-section + .sidebar-section {
    border-top: 1px solid var(--border-color);
  }

  .section-heading {
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--text-secondary);
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
    color: var(--primary-on-surface);
  }

  .nav-item.active {
    background-color: var(--primary-light);
    color: var(--primary-on-surface);
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
    display: inline-flex;
    align-items: center;
    color: var(--warning-color);
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
    font-weight: var(--font-weight-semibold);
    line-height: 1.4;
    text-align: center;
  }

  :global([data-theme='dark']) .nav-badge {
    background: rgba(245, 158, 11, 0.22);
    color: #fbbf24;
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

  .section-title-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0 0 0.25rem 0;
  }

  .section-title-row .section-title {
    margin: 0;
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
    font-size: 0.8125rem;
  }

  .form-control:disabled {
    opacity: 0.6;
    cursor: not-allowed;
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
    background-color: var(--primary-color);
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
  @media (max-width: 768px) {
    .mobile-section-nav {
      display: block;
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
