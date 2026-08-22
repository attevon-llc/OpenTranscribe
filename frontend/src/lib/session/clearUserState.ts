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
 * - Document upload queue (in-flight)
 * - API response cache (apiCache: file pages, tags, speakers, collections, groups)
 * - Thumbnail cache (blob URLs)
 * - Presigned media URL cache
 * - In-memory notification panel
 * - Recording blob (if in progress)
 * - Speaker color mappings
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
    import('$lib/services/documentUploadService').then(({ documentUploadService }) =>
      documentUploadService.reset()
    ),
    import('$stores/gallery').then(({ galleryStore }) => galleryStore.resetFilters()),
    import('$stores/search').then(({ searchStore }) => searchStore.reset()),
    import('$stores/sharing').then(({ sharingStore }) => sharingStore.reset()),
    import('$stores/llmStatus').then(({ llmStatusStore }) => llmStatusStore.reset()),
    import('$stores/settingsModalStore').then(({ settingsModalStore }) =>
      settingsModalStore.reset()
    ),
    import('$stores/transcriptStore').then(({ transcriptStore }) => transcriptStore.clear()),
    import('$stores/groups').then(({ groupsStore }) => groupsStore.reset()),
    import('$stores/downloads').then(({ downloadStore }) => downloadStore.reset()),
    import('$stores/notifications').then(({ clearAllNotifications }) => clearAllNotifications()),
    // Also aborts any in-flight stream: logging out mid-answer must not keep
    // streaming one user's conversation into the next user's session.
    import('$stores/chat').then(({ chatStore }) => chatStore.reset()),

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
    // apiCache holds the previous user's DATA, not just derived assets, and its keys
    // are not user-scoped ('tags:all', 'collections:all', 'status:summary',
    // files:page:N:hash, prefetch:file:<uuid>, ...). Until this line existed,
    // apiCache.clear() had zero call sites: User B logging in in the same tab saw
    // User A's file list, speakers, collections, tags and groups for up to the 5 min
    // TTL, because an SPA login does not reload the module holding the Map.
    import('$lib/apiCache').then(({ apiCache }) => apiCache.clear()),
    import('$lib/thumbnailCache').then(({ clearThumbnailCache }) => clearThumbnailCache()),
    import('$lib/api/mediaUrl').then(({ clearMediaUrlCache }) => clearMediaUrlCache()),
    import('$stores/speakerColors').then(({ clearSpeakerColorMappings }) =>
      clearSpeakerColorMappings()
    ),
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
  ]);

  // ── localStorage keys that hold user data ──
  // These are cleared synchronously after async cleanup.
  // Preferences (theme, locale, view mode, etc.) are NOT cleared.
  const userDataKeys = [
    'notifications', // Websocket notification queue
    'upload_queue', // Persisted upload queue
    'opentr:uploadPreviousValues', // Remembered upload choices
  ];
  for (const key of userDataKeys) {
    try {
      localStorage.removeItem(key);
    } catch {
      // Private browsing / quota errors — ignore
    }
  }
}
