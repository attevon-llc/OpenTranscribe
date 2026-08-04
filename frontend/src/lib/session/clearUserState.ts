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
 * (local, Keycloak callback, PKI, MFA) so the new user starts clean.
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
    import('$lib/thumbnailCache').then(({ clearThumbnailCache }) => clearThumbnailCache()),
    import('$lib/api/mediaUrl').then(({ clearMediaUrlCache }) => clearMediaUrlCache()),
    import('$stores/speakerColors').then(({ clearSpeakerColorMappings }) =>
      clearSpeakerColorMappings()
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
