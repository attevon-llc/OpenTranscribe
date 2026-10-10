/**
 * Centralized user-session state cleanup.
 *
 * This module is the SINGLE SOURCE OF TRUTH for everything that must be
 * cleared when a user logs in or out. It prevents data leaks between
 * sessions on the same device (e.g., User A logs out → User B logs in
 * in the same browser without a full page reload).
 *
 * Add new stores/caches here whenever they are created. Missing a cleanup
 * here is a data-leak bug.
 *
 * Imports are lazy (dynamic import) to avoid circular dependencies with
 * `stores/auth.ts` which calls this module from `logout()`.
 */

/**
 * Drop every cache that holds TENANT data, without ending the login (issue #1122).
 *
 * A support session switches which tenant the same login reads, so both edges of it
 * (start and end) must forget what the other side loaded. This is the same set
 * `clearUserState` clears minus auth, toasts, uploads, recording and capabilities. A
 * new tenant-data cache belongs HERE, so the logout path and the session-edge path
 * cannot drift apart.
 */
export async function purgeTenantDataCaches(): Promise<void> {
  await Promise.allSettled([
    // apiCache holds DATA, not just derived assets, and its keys are not user-scoped
    // ('tags:all', 'collections:all', 'status:summary', files:page:N:hash,
    // prefetch:file:<uuid>, ...). Until it was cleared here, User B logging in in the same
    // tab saw User A's file list, speakers, collections, tags and groups for up to the
    // 5 min TTL, because an SPA login does not reload the module holding the Map.
    import('$lib/apiCache').then(({ apiCache }) => apiCache.clear()),
    import('$lib/thumbnailCache').then(({ clearThumbnailCache }) => clearThumbnailCache()),
    import('$lib/api/mediaUrl').then(({ clearMediaUrlCache }) => clearMediaUrlCache()),
    import('$stores/gallery').then(({ galleryStore }) => galleryStore.resetFilters()),
    import('$stores/search').then(({ searchStore }) => searchStore.reset()),
    import('$stores/transcriptStore').then(({ transcriptStore }) => transcriptStore.clear()),
    import('$stores/chat').then(({ chatStore }) => chatStore.reset()),
  ]);
}

/**
 * Clear all user-specific state across the app.
 *
 * Call this from `auth.ts` logout() and at the start of any login flow
 * (local, OIDC callback, PKI, MFA) so the new user starts clean.
 *
 * Preserves:
 * - Theme (user preference)
 * - Locale/language (user preference)
 * - Gallery view mode (UI preference)
 * - Upload manager position (UI preference)
 * - Speaker sections collapse state (UI preference)
 * - Recording settings (device/quality preferences)
 *
 * Clears:
 * - All Svelte stores holding user data (files, searches, shares, etc.)
 * - WebSocket connection & notifications
 * - Upload queue (in-flight + persisted)
 * - API response cache (apiCache: file pages, tags, speakers, collections, groups)
 * - Thumbnail cache (blob URLs)
 * - Presigned media URL cache
 * - In-memory notification panel
 * - Recording blob (if in progress)
 * - Previous upload values (localStorage)
 */
export async function clearUserState(): Promise<void> {
  // Run all cleanup in parallel — each is independent and best-effort.
  // Failures are logged but don't block logout/login.
  await Promise.allSettled([
    // ── Svelte stores ──
    import('$stores/toast').then(({ toastStore }) => toastStore.clear()),
    import('$stores/websocket').then(({ websocketStore }) => websocketStore.clearAll()),
    import('$stores/uploads').then(({ uploadsStore }) => uploadsStore.reset()),
    // Gallery, search, transcript, chat (also aborts an in-flight stream: logging out
    // mid-answer must not keep streaming one user's conversation into the next user's
    // session), apiCache, thumbnails and presigned media URLs: see the function above.
    purgeTenantDataCaches(),
    import('$stores/sharing').then(({ sharingStore }) => sharingStore.reset()),
    import('$stores/llmStatus').then(({ llmStatusStore }) => llmStatusStore.reset()),
    import('$stores/speakerAttributePrefs').then(({ resetSpeakerAttributePrefs }) =>
      resetSpeakerAttributePrefs()
    ),
    import('$stores/settingsModalStore').then(({ settingsModalStore }) =>
      settingsModalStore.reset()
    ),
    // A support-access grant must not outlive the login that started it (issue #1122).
    import('$stores/supportSession').then(({ supportSession }) => supportSession.end('logout')),
    import('$stores/groups').then(({ groupsStore }) => groupsStore.reset()),
    import('$stores/downloads').then(({ downloadStore }) => downloadStore.reset()),
    import('$stores/notificationsPanel').then(({ clearAllNotifications }) =>
      clearAllNotifications()
    ),

    // ── Recording (stops tracks, closes audio context, clears blob) ──
    import('$stores/recording').then(({ recordingManager }) => {
      try {
        recordingManager.stopRecording();
      } catch {
        /* already stopped */
      }
      recordingManager.clearRecording();
    }),

    // ── Caches outside stores ──
    // (apiCache, thumbnails and media URLs are in purgeTenantDataCaches() above.)
    // Capabilities are TIER-SCOPED in the cloud edition and `loadCapabilities()`
    // has a single call site (routes/+layout.svelte onMount), which an SPA login
    // never re-runs. Without this reset User B inherited User A's enabled-surface
    // map until a hard reload. Each login path re-fetches after setReady(true).
    import('$stores/capabilities').then(({ resetCapabilities }) => resetCapabilities()),
    // `hosts_with_stored_credentials` in this cache is PER-USER, and `loaded` is a
    // once-only latch, so the next session never re-fetched it.
    import('$lib/services/configService').then(({ resetProtectedMediaAuthConfig }) =>
      resetProtectedMediaAuthConfig()
    ),
    // The idle guard belongs to the session that is ending (issue #1106).
    import('$lib/auth/sessionTimeouts').then(({ stopSessionTimeouts }) => stopSessionTimeouts()),
  ]);

  // ── localStorage keys that hold user data ──
  // These are cleared synchronously after async cleanup.
  // Preferences (theme, locale, view mode, etc.) are NOT cleared.
  const userDataKeys = [
    'notifications', // Websocket notification queue
    'upload_queue', // Persisted upload queue
    'opentr:uploadPreviousValues', // Remembered upload choices
    // First-seen time of an external session. Left behind, the NEXT session would
    // inherit it and hit the absolute timeout at once.
    'opentr:externalSessionFirstSeen',
  ];
  for (const key of userDataKeys) {
    try {
      localStorage.removeItem(key);
    } catch {
      // Private browsing / quota errors — ignore
    }
  }
  // Belt and braces for the support-session store's own teardown above (its import can
  // fail): the tab-scoped grant pointer must never survive a logout.
  try {
    sessionStorage.removeItem('opentr:supportSession');
  } catch {
    // Storage blocked — nothing to clear
  }
}
