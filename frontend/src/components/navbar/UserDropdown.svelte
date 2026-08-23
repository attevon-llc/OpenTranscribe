<script context="module" lang="ts">
  export interface NavbarUser {
    full_name?: string | null;
    email?: string | null;
    auth_type?: string | null;
    role?: string | null;
  }
</script>

<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import { t } from '$stores/locale';
  import { getFlowerUrl } from '$lib/utils/url';

  /** The currently signed-in user (null when unauthenticated). */
  export let user: NavbarUser | null = null;

  // Flower exposes task arguments (file/user IDs) and worker topology, so the
  // entry is admin-only. Cosmetic only — nginx auth_request is the real gate.
  $: isAdmin = user?.role === 'admin' || user?.role === 'super_admin';

  const dispatch = createEventDispatcher<{
    open: void;
    openSettings: void;
    logout: void;
    itemSelected: void;
  }>();

  // User dropdown state
  /** @type {boolean} */
  let showDropdown = false;

  /** @type {HTMLDivElement | null} */
  let dropdownRef: HTMLDivElement | null = null;

  /**
   * Toggle the user dropdown menu
   * @param {MouseEvent} event - The mouse click event
   */
  function toggleDropdown(event: MouseEvent) {
    event.stopPropagation(); // Prevent event from bubbling up
    showDropdown = !showDropdown;
    // Notify parent so it can close the notifications panel when opening
    if (showDropdown) {
      dispatch('open');
    }
  }

  /** Close the dropdown (used by the trigger setter from the parent). */
  export function close() {
    showDropdown = false;
  }

  function handleSettings() {
    showDropdown = false;
    dispatch('itemSelected');
    dispatch('openSettings');
  }

  function handleDocs() {
    showDropdown = false;
    dispatch('itemSelected');
  }

  function handleFlower() {
    // Dynamically construct Flower URL from current location
    const url = getFlowerUrl();

    // Open Flower in a new tab with the correct URL. 'noopener' — unlike
    // <a target="_blank">, window.open() gets no implicit noopener, so the new
    // tab would otherwise keep a live window.opener handle back into the SPA.
    window.open(url, '_blank', 'noopener');
    showDropdown = false;
    dispatch('itemSelected');
  }

  function handleLogout() {
    dispatch('logout');
  }

  /**
   * Handle clicks outside the dropdown to close it
   * @param {MouseEvent} event - The mouse event
   */
  function handleClickOutside(event: MouseEvent) {
    if (dropdownRef && event.target && !dropdownRef.contains(event.target as Node)) {
      showDropdown = false;
    }
  }

  onMount(() => {
    document.addEventListener('click', handleClickOutside);
  });

  onDestroy(() => {
    document.removeEventListener('click', handleClickOutside);
  });
</script>

<!-- User profile dropdown -->
<div class="user-dropdown" bind:this={dropdownRef}>
  <button
    class="user-button"
    on:click={toggleDropdown}
    aria-label={$t('nav.userMenuTooltip')}
    aria-haspopup="menu"
    aria-expanded={showDropdown}
    title={$t('nav.userMenuTooltip')}
  >
    <div class="user-avatar">
      <!-- First letter of full name as avatar -->
      {#if user && user.full_name}
        {user.full_name[0].toUpperCase()}
      {:else}
        U
      {/if}
    </div>
    <span class="username">{user ? user.full_name : $t('nav.user')}</span>
    {#if user?.auth_type === 'pki'}
      <div class="pki-badge" title={$t('nav.pkiAuthenticated')}>
        <svg class="shield-icon" viewBox="0 0 24 24" width="16" height="16">
          <path fill="#059669" d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4z"/>
          <path fill="white" d="M10 17l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z"/>
        </svg>
      </div>
    {/if}
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="dropdown-icon">
      <polyline points="6 9 12 15 18 9"></polyline>
    </svg>
  </button>

  {#if showDropdown}
    <div class="dropdown-menu">
      <div class="dropdown-header">
        <span>{$t('nav.signedInAs')}</span>
        <strong>{user ? user.email : $t('nav.user')}</strong>
      </div>
      <div class="dropdown-divider"></div>
      <button
        class="dropdown-item"
        on:click={handleSettings}
        title={$t('nav.settings')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="3"></circle>
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
        </svg>
        <span>{$t('nav.settings')}</span>
      </button>

      <a
        href="/docs/"
        target="_blank"
        rel="noopener noreferrer"
        class="dropdown-item"
        on:click={handleDocs}
        title={$t('nav.documentation')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"></path>
          <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"></path>
        </svg>
        <span>{$t('nav.documentation')}</span>
      </a>

      {#if isAdmin}
        <button
          class="dropdown-item"
          on:click={handleFlower}
          aria-label={$t('nav.flowerDashboard')}
          title={$t('nav.flowerDashboard')}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
          </svg>
          <span>{$t('nav.flowerDashboard')}</span>
        </button>
      {/if}
      <div class="dropdown-divider"></div>
      <button
        class="dropdown-item logout"
        on:click={handleLogout}
        title={$t('nav.logout')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>
          <polyline points="16 17 21 12 16 7"></polyline>
          <line x1="21" y1="12" x2="9" y2="12"></line>
        </svg>
        {$t('nav.logout')}
      </button>
    </div>
  {/if}
</div>

<style>
  /* User Dropdown Styles */
  .user-dropdown {
    position: relative;
  }

  .user-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: transparent;
    border: none;
    cursor: pointer;
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    color: var(--text-color);
    font-family: inherit;
    font-size: 1rem;
  }

  .user-button:hover {
    background-color: rgba(0, 0, 0, 0.05);
  }

  .user-avatar {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border-radius: 50%;
    background-color: #3b82f6;
    color: white;
    font-weight: 600;
  }

  .username {
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 150px;
  }

  .dropdown-icon {
    margin-left: 0.25rem;
    opacity: 0.7;
  }

  .pki-badge {
    display: flex;
    align-items: center;
    justify-content: center;
    margin-left: 0.25rem;
  }

  .pki-badge .shield-icon {
    width: 16px;
    height: 16px;
    flex-shrink: 0;
  }

  .dropdown-menu {
    position: absolute;
    top: 100%;
    right: 0;
    width: 240px;
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1);
    overflow: hidden;
    margin-top: 0.5rem;
    z-index: 1000;
    padding: 0.5rem 0;
    min-width: 200px;
    display: flex;
    flex-direction: column;
  }

  .dropdown-header {
    padding: 0.75rem 1rem;
    color: var(--text-color-secondary);
    font-size: 0.8rem;
    line-height: 1.4;
    white-space: normal;
  }

  .dropdown-header strong {
    display: block;
    color: var(--text-color);
    font-weight: 600;
    margin-top: 0.25rem;
    word-break: break-all;
  }

  .dropdown-divider {
    height: 1px;
    background-color: var(--border-color);
    margin: 0.125rem 0;
    border: none;
  }


  .dropdown-item {
    display: flex !important;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 1rem;
    color: var(--text-color) !important;
    text-decoration: none !important;
    transition: all 0.2s ease;
    font-size: 0.9rem;
    font-weight: 500;
    border: none;
    width: calc(100% - 1rem);
    text-align: left;
    background-color: transparent;
    cursor: pointer;
    font-family: inherit;
    box-sizing: border-box;
    margin: 0.125rem 0.5rem;
    border-radius: 6px;
    white-space: nowrap;
    position: relative;
  }

  /* Override any global link styles specifically for dropdown items */
  .dropdown-menu :global(a.dropdown-item) {
    color: var(--text-color) !important;
    text-decoration: none !important;
    display: flex !important;
    align-items: center !important;
    gap: 0.75rem !important;
    padding: 0.5rem 1rem !important;
    margin: 0.125rem 0.5rem !important;
    width: calc(100% - 1rem) !important;
    font-weight: 500 !important;
    border-radius: 6px !important;
    background-color: transparent !important;
    transition: all 0.2s ease !important;
  }

  .dropdown-menu :global(a.dropdown-item:hover) {
    color: var(--primary-color) !important;
    text-decoration: none !important;
    background-color: var(--hover-color, rgba(0, 0, 0, 0.05)) !important;
    transform: translateX(2px) !important;
  }

  .dropdown-menu :global(a.dropdown-item:visited) {
    color: var(--text-color) !important;
    text-decoration: none !important;
  }

  .dropdown-menu :global(a.dropdown-item:visited:hover) {
    color: var(--primary-color) !important;
    text-decoration: none !important;
  }

  .dropdown-item svg {
    width: 16px;
    height: 16px;
    flex-shrink: 0;
    opacity: 0.7;
    transition: all 0.2s ease;
  }

  /* Ensure SVGs in Link components behave the same as button SVGs */
  .dropdown-menu :global(a.dropdown-item svg) {
    width: 16px !important;
    height: 16px !important;
    flex-shrink: 0 !important;
    opacity: 0.7 !important;
    transition: all 0.2s ease !important;
  }

  .dropdown-menu :global(a.dropdown-item:hover svg) {
    opacity: 1 !important;
    color: var(--primary-color) !important;
  }

  .dropdown-item:hover {
    background-color: var(--hover-color, rgba(0, 0, 0, 0.05));
    color: var(--primary-color);
    transform: translateX(2px);
  }

  .dropdown-item:hover svg {
    opacity: 1;
    color: var(--primary-color);
  }

  .dropdown-item:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: -2px;
  }

  .dropdown-item.logout {
    color: var(--error-color, #ef4444);
    margin-top: 0.125rem;
  }

  .dropdown-item.logout svg {
    opacity: 0.8;
  }

  .dropdown-item.logout:hover {
    background-color: rgba(239, 68, 68, 0.1);
    color: var(--error-color, #dc2626);
    transform: translateX(2px);
  }

  .dropdown-item.logout:hover svg {
    opacity: 1;
    color: var(--error-color, #dc2626);
  }

  /* Active state for dropdown items */
  .dropdown-item:active {
    transform: translateX(1px);
    background-color: var(--primary-color-light, rgba(59, 130, 246, 0.1));
  }

  /* Improved spacing between groups */
  .dropdown-item + .dropdown-divider {
    margin-top: 0.25rem;
  }

  .dropdown-divider + .dropdown-item {
    margin-top: 0.125rem;
  }

  /* Hide username + chevron.
     ⚠️ Coordinated with the identical threshold in ../Navbar.svelte, which
     drops the navbar gap to 1.5rem. Both must move together: raising only the
     Navbar one left the username visible from 1281-1500px, where the bar still
     needed 1355px and still overflowed. Measured while fixing issue #452. */
  @media (max-width: 1500px) {
    .username {
      display: none;
    }

    .dropdown-icon {
      display: none;
    }
  }

  @media (max-width: 768px) {
    /* Mobile dropdown adjustments */
    .dropdown-menu {
      width: 220px;
      right: -10px;
    }

    .dropdown-item {
      padding: 0.625rem 1rem;
      margin: 0.125rem 0.25rem;
      font-size: 0.95rem;
    }

    .dropdown-item:hover {
      transform: none; /* Disable transform on mobile for better touch experience */
    }

    /* Hide username text on mobile, show avatar only */
    .username {
      display: none;
    }

    .dropdown-icon {
      display: none;
    }

    .user-button {
      gap: 0.25rem;
      padding: 0.25rem;
    }
  }

  /* Reduced motion preferences */
  @media (prefers-reduced-motion: reduce) {
    .dropdown-item,
    .dropdown-item svg {
      transition: none;
    }

    .dropdown-item:hover {
      transform: none;
    }
  }

  /* High contrast mode support */
  @media (prefers-contrast: high) {
    .dropdown-item {
      border: 1px solid transparent;
    }

    .dropdown-item:hover {
      border-color: var(--primary-color);
      background-color: var(--hover-color);
    }

    .dropdown-item.logout:hover {
      border-color: var(--error-color);
    }
  }
</style>
