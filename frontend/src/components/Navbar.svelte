<script lang="ts">
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import { user, logout, fetchUserInfo } from "../stores/auth";
  import { onMount, onDestroy } from "svelte";
  import NotificationsPanel from "./NotificationsPanel.svelte";
  import ThemeToggle from "./ThemeToggle.svelte";
  import AboutModal from "./AboutModal.svelte";
  import NavbarBrand from "$components/navbar/NavbarBrand.svelte";
  import UserDropdown from "$components/navbar/UserDropdown.svelte";
  import UsageBadge from "$lib/cloud/components/UsageBadge.svelte";
  import UpgradeButton from "$lib/cloud/components/UpgradeButton.svelte";
  import { isCloudEdition } from "$lib/edition";
  import { refreshUsage, refreshBilling } from "$lib/cloud";

  // Import the centralized notification store
  import { showNotificationsPanel, toggleNotificationsPanel, notifications } from '../stores/notifications';
  import { unreadCount } from '../stores/websocket';

  // Import recording store
  import { recordingStore, recordingManager } from '../stores/recording';

  // Import upload store for background uploads
  import { uploadsStore } from '../stores/uploads';
  import { toastStore } from '../stores/toast';

  // Import gallery store for modern state management
  import { galleryStore, galleryState } from '../stores/gallery';

  // Import settings modal store
  import { settingsModalStore } from '../stores/settingsModalStore';

  // Import i18n
  import { t } from '../stores/locale';
  import { prefetchSpeakersData, prefetchFileStatusData } from '$lib/prefetch';

  // Gallery state detection based on location
  $: isGalleryPage = $page.url.pathname === '/' || ($page.url.pathname as string) === '';

  // Recording control popup state
  let showRecordingControls = false;

  // About modal state
  let showAboutModal = false;

  // Mobile menu state
  let mobileMenuOpen = false;

  // Close mobile menu on route change
  $: if ($page.url.pathname) mobileMenuOpen = false;

  // Reactive recording state from store - use store directly
  $: hasActiveRecording = $recordingStore.hasActiveRecording;
  $: isRecording = $recordingStore.isRecording;
  $: recordingDuration = $recordingStore.recordingDuration;
  $: recordingStartTime = $recordingStore.recordingStartTime;
  $: isPaused = $recordingStore.isPaused;
  $: recordedBlob = $recordingStore.recordedBlob;

  // Reactive statements for active page detection
  $: currentPath = $page.url.pathname;
  $: isGalleryActive = currentPath === '/' || (currentPath as string) === '';
  $: isTasksActive = currentPath === '/file-status' || currentPath.startsWith('/file-status');
  $: isSpeakersActive = currentPath === '/speakers' || currentPath.startsWith('/speakers/');
  $: showGalleryLink = !isGalleryActive && !isTasksActive; // Show gallery link when not on gallery or tasks

  // User dropdown component reference (state lives in the child)
  let userDropdown: UserDropdown;

  // Notification panel state
  let showNotifs = false;
  const unsubscribe = showNotificationsPanel.subscribe(value => {
    showNotifs = value;
  });

  /**
   * Handle user logout
   */
  function handleLogout() {
    logout();
    goto("/login");
  }

  /**
   * Close the notifications panel when the user dropdown opens.
   */
  function handleUserDropdownOpen() {
    if (showNotifs) {
      showNotificationsPanel.set(false);
    }
  }

  /**
   * Open settings from the user dropdown.
   */
  function handleOpenSettings() {
    mobileMenuOpen = false;
    settingsModalStore.open('system-statistics');
  }

  /**
   * Close the mobile menu when a user-dropdown item is selected.
   */
  function handleUserMenuItemSelected() {
    mobileMenuOpen = false;
  }

  /**
   * Toggle the notifications panel
   * @param {MouseEvent} event - The mouse click event
   */
  function handleToggleNotifications(event: MouseEvent) {
    event.stopPropagation(); // Prevent event from bubbling up
    toggleNotificationsPanel();
    // Close user dropdown when opening notifications
    if (!showNotifs) {
      userDropdown?.close();
    }
  }

  /**
   * Toggle recording controls popup
   */
  function toggleRecordingControls(event: MouseEvent) {
    event.stopPropagation();
    showRecordingControls = !showRecordingControls;
    // Close notifications panel when opening recording controls
    if (showRecordingControls && showNotifs) {
      showNotificationsPanel.set(false);
    }
  }

  /**
   * Stop recording
   */
  function handleStopRecording(event: MouseEvent) {
    event.stopPropagation();
    recordingManager.stopRecording();
    // Explicitly keep popup open to show upload/delete options
    showRecordingControls = true;
  }

  /**
   * Pause/resume recording
   */
  function handleTogglePause(event: MouseEvent) {
    event.stopPropagation();
    if (isPaused) {
      recordingManager.resumeRecording();
    } else {
      recordingManager.pauseRecording();
    }
  }

  /**
   * Navigate to recording modal (fallback option)
   */
  function handleOpenRecordingModal(event: MouseEvent) {
    event.stopPropagation();
    goto('/');
    setTimeout(() => {
      window.dispatchEvent(new CustomEvent('openAddMediaModal', {
        detail: { activeTab: 'record' }
      }));
    }, 100);
    showRecordingControls = false;
  }

  /**
   * Upload recorded audio directly from popup using background upload service
   */
  function handleUploadRecording(event: MouseEvent) {
    event.stopPropagation();
    if (recordedBlob) {
      // Generate filename
      const filename = `recording_${new Date().toISOString().replace(/[:.]/g, '-')}.webm`;

      // Add to upload queue using background service
      uploadsStore.addRecording(recordedBlob, filename);

      // Show success toast
      toastStore.success($t('nav.uploadStarted', { filename }));

      // Clear recording after queuing upload
      recordingManager.clearRecording();
      showRecordingControls = false; // Close popup after upload
    }
  }

  /**
   * Delete recorded audio
   */
  function handleDeleteRecording(event: MouseEvent) {
    event.stopPropagation();
    recordingManager.clearRecording();
    showRecordingControls = false; // Close popup after delete
  }

  // Gallery control functions using store
  function handleTabChange(tab: 'gallery' | 'status') {
    galleryStore.setActiveTab(tab);
  }

  /**
   * Format recording duration for display with consistent width
   */
  function formatDuration(seconds: number) {
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;

    // Always show HH:MM:SS format for consistent width
    return `${hours.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  }

  // Make recording status reactive
  $: recordingStatusClass = (() => {
    if (isRecording && !isPaused) {
      return 'recording-live'; // Red - Recording in progress with pulse
    } else {
      return 'recording-standby'; // Green - Paused, stopped, or ready
    }
  })();

  /**
   * Get recording status title text
   */
  function getRecordingStatusTitle() {
    if (isRecording && !isPaused) {
      return $t('nav.recordingLive');
    } else if (isRecording && isPaused) {
      return $t('nav.recordingPaused');
    } else if (recordedBlob) {
      return $t('nav.recordingComplete');
    } else {
      return $t('nav.recordingStandby');
    }
  }

  // Recording controls ref for click outside detection
  let recordingControlsRef: HTMLElement | null = null;

  /**
   * Handle clicks outside the recording controls to close them
   * @param {MouseEvent} event - The mouse event
   */
  function handleClickOutside(event: MouseEvent) {
    // Close recording controls when clicking outside (but not inside the popup)
    if (showRecordingControls && recordingControlsRef && event.target && !recordingControlsRef.contains(event.target as Node)) {
      showRecordingControls = false;
    }
  }

  function closeMobileMenu() {
    mobileMenuOpen = false;
  }

  // Setup and cleanup event listeners
  onMount(() => {
    document.addEventListener('click', handleClickOutside);

    // Refresh user data to ensure we have the latest profile information
    // This ensures the navbar always shows the most up-to-date user info
    fetchUserInfo().catch(err => {
      console.error('Error refreshing user data in Navbar:', err);
    });

    // Cloud edition: prime the usage/billing stores so the navbar badge +
    // upgrade CTA render. Best-effort — failures leave the badge hidden.
    if (isCloudEdition) {
      refreshUsage().catch(() => {});
      refreshBilling().catch(() => {});
    }
  });

  onDestroy(() => {
    document.removeEventListener('click', handleClickOutside);
    unsubscribe();
  });
</script>

<nav class="navbar">
  <div class="navbar-container">
    <!-- Far Left: OpenTranscribe Logo -->
    <NavbarBrand on:about={() => showAboutModal = true} />

    <!-- Left side: Gallery tabs (only on gallery page) -->
    {#if isGalleryPage}
      <div class="gallery-tabs">
        <button
          class="tab-button {$galleryState.activeTab === 'gallery' ? 'active' : ''}"
          on:click={() => handleTabChange('gallery')}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
            <circle cx="8.5" cy="8.5" r="1.5"></circle>
            <polyline points="21 15 16 10 5 21"></polyline>
          </svg>
          <span class="tab-label">{$t('nav.gallery')}</span>
        </button>
        <button
          class="tab-button {$galleryState.activeTab === 'status' ? 'active' : ''}"
          on:click={() => handleTabChange('status')}
          on:mouseenter={prefetchFileStatusData}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
            <polyline points="14 2 14 8 20 8"></polyline>
            <line x1="16" y1="13" x2="8" y2="13"></line>
            <line x1="16" y1="17" x2="8" y2="17"></line>
            <polyline points="10 9 9 9 8 9"></polyline>
          </svg>
          <span class="tab-label">{$t('nav.fileStatus')}</span>
        </button>
      </div>
    {/if}

    <!-- Spacer to push right elements to far right -->
    <div class="navbar-spacer"></div>

    <div class="nav-links" class:open={mobileMenuOpen}>
      <!-- Gallery link - only show when not on gallery/file pages -->
      {#if showGalleryLink}
        <a
          href="/"
          title={$t('nav.backToGallery')}
          class="nav-link"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
            <circle cx="8.5" cy="8.5" r="1.5"></circle>
            <polyline points="21 15 16 10 5 21"></polyline>
          </svg>
          <span class="nav-label">{$t('nav.backToGallery')}</span>
        </a>
      {/if}

      <!-- Search link -->
      <a
        href="/search"
        title={$t('nav.search')}
        class="nav-link"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="8"></circle>
          <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
        </svg>
        <span class="nav-label">{$t('nav.search')}</span>
      </a>

      <!-- Speakers link -->
      <a
        href="/speakers"
        title={$t('nav.speakers')}
        class="nav-link"
        class:active={isSpeakersActive}
        on:mouseenter={prefetchSpeakersData}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path>
          <circle cx="9" cy="7" r="4"></circle>
          <path d="M23 21v-2a4 4 0 0 0-3-3.87"></path>
          <path d="M16 3.13a4 4 0 0 1 0 7.75"></path>
        </svg>
        <span class="nav-label">{$t('nav.speakers')}</span>
      </a>

      <!-- Notifications button -->
      <button
        class="notifications-btn"
        on:click={handleToggleNotifications}
        aria-label={$unreadCount > 0 ? $t('nav.notificationsWithUnread', { count: $unreadCount }) : $t('nav.notificationsTooltip')}
        title={$unreadCount > 0 ? $t('nav.notificationsWithUnread', { count: $unreadCount }) : $t('nav.notificationsTooltip')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>
          <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
        </svg>
        {#if $unreadCount > 0}
          <span class="notification-badge">{$unreadCount}</span>
        {/if}
      </button>

      <!-- Cloud edition: usage badge + upgrade CTA (each self-gates / hides
           when not applicable). Sits between notifications and the user menu. -->
      {#if isCloudEdition}
        <UsageBadge />
        <UpgradeButton />
      {/if}

      <!-- Recording indicator (when active) -->
      {#if hasActiveRecording}
        <div class="recording-container">
          <button
            class="recording-indicator {recordingStatusClass}"
            on:click={toggleRecordingControls}
            title={$t('nav.recordingTooltip', { status: getRecordingStatusTitle(), duration: formatDuration(recordingDuration) })}
          >
            <div class="recording-pulse {recordingStatusClass}"></div>
            <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="mic-icon">
              <path d="M12 1a4 4 0 0 0-4 4v7a4 4 0 0 0 8 0V5a4 4 0 0 0-4-4z"></path>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
              <line x1="12" y1="19" x2="12" y2="23"></line>
              <line x1="8" y1="23" x2="16" y2="23"></line>
            </svg>
            <span class="recording-time">{formatDuration(recordingDuration)}</span>
          </button>

          {#if showRecordingControls}
            <div class="recording-controls-popup" bind:this={recordingControlsRef}>
              <div class="popup-header">
                <div class="recording-status">
                  {#if isRecording}
                    <div class="status-dot {isPaused ? 'paused' : 'recording'}"></div>
                    <span>{isPaused ? $t('nav.paused') : $t('nav.recording')}</span>
                    <span class="duration">{formatDuration(recordingDuration)}</span>
                  {:else if recordedBlob}
                    <div class="status-dot completed"></div>
                    <span>{$t('nav.recordingCompleteStatus')}</span>
                    <span class="duration">{formatDuration(recordingDuration)}</span>
                  {:else}
                    <div class="status-dot idle"></div>
                    <span>{$t('nav.readyToRecord')}</span>
                  {/if}
                </div>
              </div>

              <div class="popup-controls">
                {#if isRecording}
                  <!-- Recording controls -->
                  <button
                    class="control-btn pause-btn"
                    on:click={handleTogglePause}
                    aria-label={isPaused ? $t('nav.resumeRecording') : $t('nav.pauseRecording')}
                    title={isPaused ? $t('nav.resumeRecording') : $t('nav.pauseRecording')}
                  >
                    {#if isPaused}
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polygon points="5 3 19 12 5 21 5 3"></polygon>
                      </svg>
                    {:else}
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <rect x="6" y="4" width="4" height="16"></rect>
                        <rect x="14" y="4" width="4" height="16"></rect>
                      </svg>
                    {/if}
                  </button>

                  <button
                    class="control-btn stop-btn"
                    on:click={handleStopRecording}
                    aria-label={$t('nav.stopRecording')}
                    title={$t('nav.stopRecording')}
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <rect x="3" y="3" width="18" height="18"></rect>
                    </svg>
                  </button>
                {:else if recordedBlob}
                  <!-- Completed recording controls - reordered to prevent accidental deletion -->
                  <button
                    class="control-btn delete-btn"
                    on:click={handleDeleteRecording}
                    aria-label={$t('nav.deleteRecording')}
                    title={$t('nav.deleteRecording')}
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <polyline points="3 6 5 6 21 6"></polyline>
                      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                      <line x1="10" y1="11" x2="10" y2="17"></line>
                      <line x1="14" y1="11" x2="14" y2="17"></line>
                    </svg>
                  </button>

                  <button
                    class="control-btn upload-btn"
                    on:click={handleUploadRecording}
                    aria-label={$t('nav.uploadRecording')}
                    title={$t('nav.uploadRecording')}
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                      <polyline points="17 8 12 3 7 8"></polyline>
                      <line x1="12" y1="3" x2="12" y2="15"></line>
                    </svg>
                  </button>
                {/if}

                <!-- Always show expand button -->
                <button
                  class="control-btn modal-btn"
                  on:click={handleOpenRecordingModal}
                  aria-label={$t('nav.openFullRecording')}
                  title={$t('nav.openFullRecording')}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="15 3 21 3 21 9"></polyline>
                    <polyline points="9 21 3 21 3 15"></polyline>
                    <line x1="21" y1="3" x2="14" y2="10"></line>
                    <line x1="3" y1="21" x2="10" y2="14"></line>
                  </svg>
                </button>
              </div>
            </div>
          {/if}
        </div>
      {/if}

      <!-- Theme toggle switch -->
      <div class="theme-toggle-container">
        <ThemeToggle />
      </div>

      <!-- User profile dropdown -->
      <UserDropdown
        bind:this={userDropdown}
        user={$user}
        on:open={handleUserDropdownOpen}
        on:openSettings={handleOpenSettings}
        on:itemSelected={handleUserMenuItemSelected}
        on:logout={handleLogout}
      />
    </div>

    <div class="mobile-toggle">
      <button on:click|stopPropagation={() => (mobileMenuOpen = !mobileMenuOpen)} aria-label={$t('nav.menu')} aria-expanded={mobileMenuOpen}>
        <span class="sr-only">{$t('nav.menu')}</span>
        {#if mobileMenuOpen}
          <!-- X icon when open -->
          <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        {:else}
          <!-- Hamburger icon when closed -->
          <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="3" y1="12" x2="21" y2="12"></line>
            <line x1="3" y1="6" x2="21" y2="6"></line>
            <line x1="3" y1="18" x2="21" y2="18"></line>
          </svg>
        {/if}
      </button>
    </div>
  </div>
</nav>

{#if mobileMenuOpen}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="mobile-overlay" on:click={closeMobileMenu}></div>
{/if}

<!-- Add the NotificationsPanel component -->
{#if showNotifs}
  <NotificationsPanel />
{/if}

<!-- About Modal -->
<AboutModal bind:showModal={showAboutModal} />

<style>
  .navbar {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    height: calc(60px + env(safe-area-inset-top, 0px));
    padding-top: env(safe-area-inset-top, 0px);
    background-color: var(--surface-color);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    z-index: 1200;
    overflow: visible; /* Allow button hover effects to display properly */
  }

  .navbar-container {
    display: flex;
    align-items: center;
    height: 100%;
    padding: 0.75rem 1.5rem; /* Increased vertical padding to prevent button clipping on hover */
    max-width: 1600px;
    margin: 0 auto;
    gap: 3rem; /* Further increased spacing between sections */
    overflow: visible; /* Allow button hover effects to display properly */
  }

  .navbar-spacer {
    flex: 1; /* Takes up remaining space to push right elements to the right */
  }

  /* Gallery Controls */
  .gallery-tabs {
    display: flex;
    gap: 0.75rem;
    align-items: center;
  }

  /* Tab Button Styles */
  .tab-button {
    color: var(--text-color);
    background: none;
    border: none;
    padding: 0.4rem 0.8rem;
    border-radius: 6px;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-family: inherit;
    font-size: 0.9rem;
    cursor: pointer;
    position: relative;
    font-weight: 500;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .tab-button:hover {
    background-color: var(--hover-color, rgba(0, 0, 0, 0.05));
    color: var(--primary-color);
  }

  .tab-button.active {
    color: var(--primary-color, #3b82f6);
    background-color: transparent;
  }

  .tab-button.active::after {
    content: '';
    position: absolute;
    bottom: -8px;
    left: 50%;
    transform: translateX(-50%);
    width: calc(100% - 1rem);
    height: 3px;
    background-color: #3b82f6;
    border-radius: 2px;
  }

  .nav-links {
    display: flex;
    align-items: center;
    gap: 1.5rem;
  }

  .nav-link {
    color: var(--text-color);
    text-decoration: none;
    padding: 0.5rem 1rem;
    border-radius: 6px;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: none;
    border: none;
    font-family: inherit;
    font-size: 1rem;
    cursor: pointer;
    position: relative;
    font-weight: 500;
    white-space: nowrap;
  }

  .nav-link:hover {
    background-color: var(--hover-color, rgba(0, 0, 0, 0.05));
    color: var(--primary-color);
  }

  .nav-link.active {
    color: var(--primary-color);
    background-color: var(--hover-color, rgba(0, 0, 0, 0.05));
  }

  /* Focus states for accessibility */
  .nav-link:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  /* Responsive adjustments for mobile */
  @media (max-width: 768px) {
    .nav-links {
      gap: 0.5rem;
    }

    .nav-link {
      padding: 0.4rem 0.8rem;
      font-size: 0.9rem;
    }

  }

  /* High contrast mode support */
  @media (prefers-contrast: high) {
    .nav-link:hover {
      border: 1px solid var(--primary-color);
    }
  }

  /* Reduced motion support */
  @media (prefers-reduced-motion: reduce) {
    .nav-link {
      transition: none;
    }
  }


  .theme-toggle-container {
    display: flex;
    align-items: center;
    margin: 0 8px;
  }

  .notifications-btn {
    position: relative;
    background: none;
    border: none;
    padding: 8px;
    border-radius: 50%;
    cursor: pointer;
    color: var(--text-color);
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background-color 0.2s;
  }

  .notifications-btn:hover {
    background-color: rgba(0, 0, 0, 0.05);
  }

  .notification-badge {
    position: absolute;
    top: -5px;
    right: -5px;
    background-color: var(--error-color);
    color: white;
    width: 18px;
    height: 18px;
    font-size: 0.7rem;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    font-weight: bold;
  }

  .mobile-toggle {
    display: none;
  }

  .mobile-overlay {
    display: none;
  }

  @media (max-width: 768px) {
    .mobile-overlay {
      display: block;
      position: fixed;
      inset: 0;
      z-index: 1099;
      background: rgba(0, 0, 0, 0.2);
    }
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border-width: 0;
  }

  /* iPad landscape — reduce gap, hide username, keep nav labels */
  @media (max-width: 1200px) {
    .navbar-container {
      gap: 1.5rem;
      padding: 0.5rem 1rem;
    }
  }

  /* iPad portrait — icon-only nav links, tighter layout */
  @media (max-width: 1024px) {
    .navbar-container {
      gap: 1rem;
    }

    .nav-links {
      gap: 0.75rem;
    }

    .nav-link {
      padding: 0.4rem 0.6rem;
      font-size: 0.875rem;
    }

    /* Icon-only nav links on tablet portrait */
    .nav-label {
      display: none;
    }

    .tab-button {
      padding: 0.35rem 0.6rem;
      font-size: 0.85rem;
    }

    .gallery-tabs {
      gap: 0.375rem;
    }
  }

  @media (max-width: 768px) {
    .navbar-container {
      gap: 0.5rem;
      padding: 0.5rem;
    }

    .gallery-tabs {
      gap: 0.25rem;
    }

    .tab-button {
      padding: 0.3rem 0.5rem;
      font-size: 0.8rem;
    }

    .tab-button.active::after {
      bottom: -6px;
      height: 2px;
    }

    .nav-links {
      display: none;
    }

    .nav-links.open {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 0.25rem;
      position: fixed;
      top: calc(60px + env(safe-area-inset-top, 0px));
      left: 0;
      right: 0;
      background: var(--surface-color);
      border-bottom: 1px solid var(--border-color);
      padding: 0.75rem 1rem;
      z-index: 1100;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
      max-height: calc(100vh - 60px - env(safe-area-inset-top, 0px));
      max-height: calc(100dvh - 60px - env(safe-area-inset-top, 0px));
      overflow-y: auto;
    }

    /* Make nav items full-width in mobile menu */
    .nav-links.open :global(.nav-link),
    .nav-links.open :global(.nav-btn) {
      width: 100%;
      justify-content: flex-start;
      padding: 0.75rem 1rem;
      min-height: 44px;
    }

    .mobile-toggle {
      display: block;
    }

    .mobile-toggle button {
      background: transparent;
      border: none;
      cursor: pointer;
      color: var(--text-color);
      min-width: 44px;
      min-height: 44px;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    /* Mobile: user dropdown flows inline in the mobile menu.
       These reach into the child UserDropdown component, hence :global(). */
    .nav-links.open :global(.user-dropdown) {
      width: 100%;
    }

    .nav-links.open :global(.user-button) {
      width: 100%;
      justify-content: flex-start;
      padding: 0.75rem 1rem;
      min-height: 44px;
      gap: 0.5rem;
    }

    /* Show username in the mobile menu for clarity */
    .nav-links.open :global(.username) {
      display: inline;
    }

    .nav-links.open :global(.dropdown-icon) {
      display: inline;
      margin-left: auto;
    }

    .nav-links.open :global(.dropdown-menu) {
      position: relative;
      width: 100%;
      top: auto;
      right: auto;
      box-shadow: none;
      border: none;
      border-top: 1px solid var(--border-color);
      border-radius: 0;
      margin-top: 0.25rem;
      background: transparent;
    }

    .nav-links.open :global(.dropdown-item) {
      width: 100%;
      min-height: 44px;
      display: flex;
      align-items: center;
      margin: 0;
      border-radius: 6px;
      padding: 0.75rem 1rem;
    }

    .nav-links.open :global(.dropdown-header) {
      padding: 0.75rem 1rem;
    }

    .nav-links.open :global(.dropdown-divider) {
      margin: 0.25rem 0;
    }
  }

  /* Extra-small screens (iPhone SE, 375px and below) */
  @media (max-width: 480px) {
    /* Hide tab label text, show icon-only tabs */
    .tab-label {
      display: none;
    }

    .tab-button {
      padding: 0.3rem 0.4rem;
    }

    .navbar-container {
      gap: 0.25rem;
      padding: 0.5rem 0.375rem;
    }

    .recording-time {
      display: none;
    }

    .recording-indicator {
      padding: 0.4rem 0.5rem;
    }

    .theme-toggle-container {
      margin: 0 2px;
    }
  }

  /* Recording Container and Indicator Styles */
  .recording-container {
    position: relative;
  }

  .recording-indicator {
    position: relative;
    border-radius: 8px;
    padding: 0.5rem 0.75rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-family: inherit;
    font-size: 0.85rem;
    font-weight: 600;
    transition: all 0.3s ease;
    overflow: hidden;
  }

  /* Studio-style recording status colors */
  .recording-indicator.recording-live {
    background: rgba(220, 38, 38, 0.15);
    border: 1px solid rgba(220, 38, 38, 0.4);
    color: #dc2626;
  }

  .recording-indicator.recording-live:hover {
    background: rgba(220, 38, 38, 0.2);
    border-color: rgba(220, 38, 38, 0.5);
    transform: scale(1.02);
    box-shadow: 0 4px 12px rgba(220, 38, 38, 0.3);
  }

  .recording-indicator.recording-standby {
    background: rgba(16, 185, 129, 0.15);
    border: 1px solid rgba(16, 185, 129, 0.4);
    color: #10b981;
  }

  .recording-indicator.recording-standby:hover {
    background: rgba(16, 185, 129, 0.2);
    border-color: rgba(16, 185, 129, 0.5);
    transform: scale(1.02);
    box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
  }

  .recording-pulse {
    position: absolute;
    top: 6px;
    left: 6px;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    animation: recordingPulse 2s infinite ease-in-out;
  }

  /* Pulse colors for different recording states */
  .recording-pulse.recording-live {
    background: #dc2626;
    animation: recordingPulseLive 1.5s infinite ease-in-out;
  }

  .recording-pulse.recording-standby {
    background: #10b981;
    animation: none; /* No pulse for standby/paused/stopped */
    opacity: 1;
  }

  .mic-icon {
    flex-shrink: 0;
    z-index: 1;
    color: currentColor; /* Use the current button color */
  }

  .recording-time {
    font-family: system-ui, -apple-system, sans-serif;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    font-size: 0.8rem;
    letter-spacing: 0.5px;
    min-width: 60px; /* Reserve space for HH:MM:SS */
    text-align: center;
    display: inline-block;
    color: currentColor; /* Use the current button color */
  }

  /* Professional recording studio pulse animations */
  @keyframes recordingPulseLive {
    0%, 100% {
      opacity: 1;
      transform: scale(1);
      box-shadow: 0 0 0 0 rgba(220, 38, 38, 0.7);
    }
    50% {
      opacity: 0.6;
      transform: scale(1.2);
      box-shadow: 0 0 0 8px rgba(220, 38, 38, 0);
    }
  }


  /* Dark theme adjustments for recording status lights */
  :global([data-theme='dark']) .recording-indicator.recording-live {
    background: rgba(239, 68, 68, 0.2);
    border-color: rgba(239, 68, 68, 0.5);
    color: #ef4444;
  }

  :global([data-theme='dark']) .recording-indicator.recording-standby {
    background: rgba(52, 211, 153, 0.2);
    border-color: rgba(52, 211, 153, 0.5);
    color: #34d399;
  }

  :global([data-theme='dark']) .recording-pulse.recording-live {
    background: #ef4444;
  }

  :global([data-theme='dark']) .recording-pulse.recording-standby {
    background: #34d399;
  }

  /* Accessibility and reduced motion */
  @media (prefers-reduced-motion: reduce) {
    .recording-pulse {
      animation: none;
      opacity: 1;
    }

    .recording-indicator:hover {
      transform: none;
    }
  }

  /* High contrast mode support */
  @media (prefers-contrast: high) {
    .recording-indicator {
      border-width: 2px;
      background: rgba(239, 68, 68, 0.2);
    }

    .recording-indicator:hover {
      background: rgba(239, 68, 68, 0.3);
    }
  }

  /* Recording Controls Popup Styles */
  .recording-controls-popup {
    position: absolute;
    top: calc(100% + 12px);
    left: 50%;
    transform: translateX(-50%);
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.15);
    padding: 1rem;
    min-width: 200px;
    z-index: 1001;
    animation: slideIn 0.2s ease-out;
  }

  /* Speech bubble triangle */
  .recording-controls-popup::before {
    content: '';
    position: absolute;
    top: -8px;
    left: 50%;
    transform: translateX(-50%);
    width: 0;
    height: 0;
    border-left: 8px solid transparent;
    border-right: 8px solid transparent;
    border-bottom: 8px solid var(--border-color);
  }

  .recording-controls-popup::after {
    content: '';
    position: absolute;
    top: -7px;
    left: 50%;
    transform: translateX(-50%);
    width: 0;
    height: 0;
    border-left: 7px solid transparent;
    border-right: 7px solid transparent;
    border-bottom: 7px solid var(--background-color);
  }

  .popup-header {
    margin-bottom: 0.75rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border-color);
  }

  .recording-status {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.9rem;
    font-weight: 600;
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .status-dot.recording {
    background: #dc2626;
    animation: recordingPulse 2s infinite ease-in-out;
  }

  .status-dot.paused {
    background: #f59e0b;
  }

  .status-dot.completed {
    background: #10b981;
  }

  .status-dot.idle {
    background: #6b7280;
  }

  .duration {
    color: var(--text-secondary);
    font-family: system-ui, -apple-system, sans-serif;
    font-variant-numeric: tabular-nums;
    font-size: 0.85rem;
    font-weight: 500;
    margin-left: auto;
    min-width: 64px; /* Reserve space for HH:MM:SS */
    text-align: right;
    display: inline-block;
  }

  .popup-controls {
    display: flex;
    gap: 0.5rem;
    justify-content: center;
  }

  .control-btn {
    background: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.5rem;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-color);
  }

  .control-btn:hover {
    background: var(--hover-color);
    border-color: var(--primary-color);
    color: var(--primary-color);
    transform: scale(1.02);
  }

  .pause-btn:hover {
    background: rgba(59, 130, 246, 0.1);
    border-color: #3b82f6;
    color: #3b82f6;
  }

  .stop-btn:hover {
    background: rgba(239, 68, 68, 0.1);
    border-color: #ef4444;
    color: #ef4444;
  }

  .upload-btn:hover {
    background: rgba(16, 185, 129, 0.1);
    border-color: #10b981;
    color: #10b981;
  }

  .delete-btn:hover {
    background: rgba(153, 27, 27, 0.1);
    border-color: #991b1b;
    color: #991b1b;
  }

  .modal-btn:hover {
    background: var(--hover-color);
    border-color: var(--primary-color);
    color: var(--primary-color);
  }

  .modal-btn:hover svg {
    color: var(--primary-color);
    stroke: var(--primary-color);
    opacity: 1;
  }

  /* Dark theme adjustments for popup */
  :global([data-theme='dark']) .recording-controls-popup {
    background: var(--background-color);
    border-color: var(--border-color);
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.3);
  }

  :global([data-theme='dark']) .popup-header {
    border-bottom-color: var(--border-color);
  }

  /* Dark theme adjustments for triangle */
  :global([data-theme='dark']) .recording-controls-popup::before {
    border-bottom-color: var(--border-color);
  }

  :global([data-theme='dark']) .recording-controls-popup::after {
    border-bottom-color: var(--background-color);
  }

  /* Responsive design */
  @media (max-width: 768px) {
    .recording-controls-popup {
      min-width: 180px;
      /* Keep centered positioning on mobile */
    }

    .control-btn {
      padding: 0.4rem;
    }

    /* Adjust triangle for smaller screens */
    .recording-controls-popup::before,
    .recording-controls-popup::after {
      border-left-width: 6px;
      border-right-width: 6px;
    }

    .recording-controls-popup::before {
      border-bottom-width: 6px;
    }

    .recording-controls-popup::after {
      border-bottom-width: 5px;
      top: -5px;
    }
  }
</style>
