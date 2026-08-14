import { writable, derived, get, type Writable } from 'svelte/store';
import * as authStore from './auth';
import { downloadStore, type DownloadState } from './downloads';
import { t } from '$stores/locale';
import axiosInstance from '$lib/axios';
import { generateId } from '$lib/utils/ids';
import { reconnectDelayMs } from '$lib/utils/backoff';
import { isCloudEdition } from '$lib/edition';

// Define notification types
export type NotificationType =
  | 'transcription_status'
  | 'document_status'
  | 'summarization_status'
  | 'redaction_status'
  | 'topic_extraction_status'
  | 'auto_label_status'
  | 'youtube_processing_status'
  | 'playlist_processing_status'
  | 'analytics_status'
  | 'download_progress'
  | 'audio_extraction_status'
  | 'connection_established'
  | 'echo'
  | 'file_upload'
  | 'file_created'
  | 'file_updated'
  | 'file_deleted'
  | 'speaker_updated'
  | 'speaker_processing_complete'
  | 'gpu_stats_update'
  | 'reindex_progress'
  | 'reindex_complete'
  | 'reindex_stopped'
  | 'migration_progress'
  | 'migration_complete'
  | 'migration_finalized'
  | 'clustering_progress'
  | 'clustering_complete'
  | 'clustering_file_complete'
  | 'attribute_migration_progress'
  | 'attribute_migration_complete'
  | 'data_integrity_progress'
  | 'data_integrity_complete'
  | 'embedding_consistency_progress'
  | 'embedding_consistency_complete'
  | 'cache_invalidate'
  | 'collection_shared'
  | 'collection_share_revoked'
  | 'collection_share_updated'
  | 'group_member_added'
  | 'group_member_removed'
  | 'enrichment_started'
  | 'enrichment_task_complete'
  | 'search_indexing_complete'
  | 'file_takedown'
  | 'file_takedown_released';

/**
 * Payload attached to a {@link Notification}. Carries the `data` object from the
 * backend WebSocket message. The known fields below are read by consumers (file
 * detail page, settings panels); the index signature keeps forward-compatibility
 * with task-specific extra fields.
 */
export interface NotificationData {
  file_id?: string;
  file_uuid?: string;
  filename?: string;
  reason?: string;
  abuse_contact_email?: string;
  status?: string;
  message?: string;
  progress?: number;
  summary?: string;
  auto_applied_count?: number;
  suggested_count?: number;
  skipped_detectors?: string[];
  scope?: string;
  task?: string;
  tasks?: string[];
  eta_seconds?: number | null;
  [key: string]: unknown;
}

// Notification interface
export interface Notification {
  id: string;
  type: NotificationType;
  title: string;
  message: string;
  timestamp: Date;
  read: boolean;
  data?: NotificationData;
  // Progressive notification fields
  progressId?: string; // Used to group progressive notifications
  currentStep?: string; // Current processing step
  progress?: {
    current: number;
    total: number;
    percentage: number;
    etaSeconds?: number | null;
    etaDisplay?: string;
  };
  status?: 'processing' | 'completed' | 'error';
  dismissible?: boolean; // false while processing
  silent?: boolean; // if true, don't show in notification panel (for gallery-only updates)
  enrichmentTasks?: string[]; // Expected enrichment tasks (from enrichment_started)
  completedEnrichments?: string[]; // Completed enrichment task names (chips)
}

// WebSocket connection status
export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error';

// Store state interface
interface WebSocketState {
  socket: WebSocket | null;
  status: ConnectionStatus;
  notifications: Notification[];
  reconnectAttempts: number;
  error: string | null;
}

// Format ETA seconds into human-readable string
function formatEtaSeconds(seconds: number | null | undefined): string | undefined {
  if (seconds == null || seconds <= 0) return undefined;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  if (minutes < 60) return secs > 0 ? `${minutes}m ${secs}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
}

// Admin task type to progressId mapping
const ADMIN_TASK_PROGRESS_IDS: Record<string, string> = {
  reindex_progress: 'admin_reindex',
  reindex_complete: 'admin_reindex',
  reindex_stopped: 'admin_reindex',
  migration_progress: 'admin_migration',
  migration_complete: 'admin_migration',
  migration_finalized: 'admin_migration',
  clustering_progress: 'admin_clustering',
  clustering_complete: 'admin_clustering',
  attribute_migration_progress: 'admin_attribute_migration',
  attribute_migration_complete: 'admin_attribute_migration',
  data_integrity_progress: 'admin_data_integrity',
  data_integrity_complete: 'admin_data_integrity',
  embedding_consistency_progress: 'admin_embedding_consistency',
  embedding_consistency_complete: 'admin_embedding_consistency',
};

// Admin task completion types
const ADMIN_COMPLETION_TYPES = new Set([
  'reindex_complete',
  'reindex_stopped',
  'migration_complete',
  'migration_finalized',
  'clustering_complete',
  'attribute_migration_complete',
  'data_integrity_complete',
  'embedding_consistency_complete',
]);

// Load notifications from localStorage if available
const loadNotificationsFromStorage = (): Notification[] => {
  if (typeof window !== 'undefined') {
    try {
      const stored = localStorage.getItem('notifications');
      if (stored) {
        const parsed: Array<Notification & { timestamp: string }> = JSON.parse(stored);
        // Convert timestamp strings back to Date objects
        return parsed.map((n) => ({
          ...n,
          timestamp: new Date(n.timestamp),
        }));
      }
    } catch (error) {
      console.warn('Failed to load notifications from localStorage:', error);
    }
  }
  return [];
};

// Save notifications to localStorage
const saveNotificationsToStorage = (notifications: Notification[]) => {
  if (typeof window !== 'undefined') {
    try {
      localStorage.setItem('notifications', JSON.stringify(notifications));
    } catch (error) {
      console.warn('Failed to save notifications to localStorage:', error);
    }
  }
};

// Initial state
const initialState: WebSocketState = {
  socket: null,
  status: 'disconnected',
  notifications: loadNotificationsFromStorage(),
  reconnectAttempts: 0,
  error: null,
};

// Create the store
function createWebSocketStore() {
  const { subscribe, set, update } = writable<WebSocketState>(initialState);

  // Keep track of reconnect timeout
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;

  // Get current state without subscribing
  const getState = (): WebSocketState => {
    let currentState: WebSocketState = initialState;
    const unsubscribe = subscribe((state) => {
      currentState = state;
    });
    unsubscribe();
    return currentState;
  };

  // Connect to WebSocket server (cookies are sent automatically)
  const connect = (_token?: string) => {
    update((state: WebSocketState) => {
      // Clean up previous connection if exists
      if (state.socket) {
        state.socket.onclose = null;
        state.socket.onerror = null;
        state.socket.onmessage = null;
        state.socket.onopen = null;
        state.socket.close();
      }

      // Clear any previous reconnect timeout
      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }

      try {
        // Always construct WebSocket URL dynamically based on current location
        // This ensures it works in dev, production, and when accessed from different computers
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host;
        const wsUrl = `${protocol}//${host}/api/ws`;

        // Create new WebSocket — httpOnly cookies are sent automatically by the browser
        const socket = new WebSocket(wsUrl);

        // Set connecting status
        state.status = 'connecting';
        state.socket = socket;
        state.error = null;

        // WebSocket event handlers
        socket.onopen = () => {
          // Community edition: authentication is handled via httpOnly cookies sent
          // during the WebSocket handshake. The backend WS handler reads the
          // access_token cookie automatically.
          //
          // Cloud edition: browsers can't set Authorization headers on a WS
          // handshake, so we authenticate with a first-message frame carrying a
          // fresh hosted-IdP token. The backend's WS external-verifier reads it.
          // We only mark connected / recover progress after auth is sent.
          if (isCloudEdition) {
            (async () => {
              try {
                const { getSessionToken } = await import('$lib/cloud');
                const sessionToken = await getSessionToken();
                if (sessionToken) {
                  socket.send(JSON.stringify({ type: 'authenticate', token: sessionToken }));
                }
              } catch (err) {
                console.error('[ws] Failed to send external authenticate frame:', err);
              }
              update((s: WebSocketState) => {
                s.status = 'connected';
                s.reconnectAttempts = 0;
                return s;
              });
              recoverActiveProgress();
            })();
            return;
          }

          update((s: WebSocketState) => {
            s.status = 'connected';
            s.reconnectAttempts = 0;
            return s;
          });

          // Recover active task progress from backend (survives page refresh/modal close)
          recoverActiveProgress();
        };

        socket.onclose = (event) => {
          update((s: WebSocketState) => {
            s.status = 'disconnected';
            s.socket = null;

            // Don't attempt to reconnect if closed cleanly or if page is hidden
            const shouldReconnect =
              event.code !== 1000 &&
              event.code !== 1001 &&
              (typeof document === 'undefined' || !document.hidden);

            if (shouldReconnect) {
              tryReconnect();
            }

            return s;
          });
        };

        socket.onerror = () => {
          update((s: WebSocketState) => {
            s.status = 'error';
            s.error = 'WebSocket connection error';
            return s;
          });
        };

        socket.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);

            // Handle different message types
            if (data.type === 'connection_established') {
              // Just a connection confirmation, no notification needed
              return;
            } else if (data.type === 'echo') {
              // Echo messages are just for debugging/heartbeat
              return;
            } else if (data.type === 'download_progress') {
              // Handle download progress messages specially
              handleDownloadProgress(data);
              return;
            } else if (data.type === 'gpu_stats_update') {
              // Silent GPU stats update — dispatch event for SettingsModal
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('gpu-stats-updated', { detail: data.data }));
              }
              return;
            } else if (data.type === 'watch_source_scan') {
              // Watch-source scan completed — dispatch event for WatchSourcesSettings
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('watch-source-scan', { detail: data.data }));
              }
              return;
            } else if (data.type === 'reindex_progress') {
              // Reindex progress update — dispatch event for SearchSettings
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('reindex-progress', { detail: data.data }));
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'reindex_complete') {
              // Reindex complete — dispatch event for SearchSettings
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('reindex-complete', { detail: data.data }));
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'reindex_stopped') {
              // Reindex stopped by user — dispatch event for SearchSettings
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('reindex-stopped', { detail: data.data }));
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'migration_progress') {
              // Embedding migration progress — dispatch event for EmbeddingMigrationSettings
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('migration-progress', { detail: data.data }));
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'migration_complete') {
              // Embedding migration complete — dispatch event for EmbeddingMigrationSettings
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('migration-complete', { detail: data.data }));
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'migration_finalized') {
              // Embedding migration finalized (index swap complete)
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('migration-finalized', { detail: data.data }));
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'attribute_migration_progress') {
              // Speaker attribute migration progress
              if (typeof window !== 'undefined') {
                window.dispatchEvent(
                  new CustomEvent('attribute-migration-progress', { detail: data.data })
                );
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'attribute_migration_complete') {
              // Speaker attribute migration complete
              if (typeof window !== 'undefined') {
                window.dispatchEvent(
                  new CustomEvent('attribute-migration-complete', { detail: data.data })
                );
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'data_integrity_progress') {
              // Data integrity progress — dispatch event for DataIntegritySettings
              if (typeof window !== 'undefined') {
                window.dispatchEvent(
                  new CustomEvent('data-integrity-progress', { detail: data.data })
                );
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'data_integrity_complete') {
              // Data integrity complete — dispatch event for DataIntegritySettings
              if (typeof window !== 'undefined') {
                window.dispatchEvent(
                  new CustomEvent('data-integrity-complete', { detail: data.data })
                );
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'embedding_consistency_progress') {
              // Embedding consistency progress — dispatch event for EmbeddingConsistencySettings
              if (typeof window !== 'undefined') {
                window.dispatchEvent(
                  new CustomEvent('embedding-consistency-progress', { detail: data.data })
                );
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'embedding_consistency_complete') {
              // Embedding consistency complete — dispatch event for EmbeddingConsistencySettings
              if (typeof window !== 'undefined') {
                window.dispatchEvent(
                  new CustomEvent('embedding-consistency-complete', { detail: data.data })
                );
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'cache_invalidate') {
              // Push-based cache invalidation from backend
              // Invalidate the in-memory apiCache and notify listening components
              import('$lib/apiCache')
                .then(({ apiCache }) => {
                  const scope = data.data?.scope || 'all';
                  apiCache.invalidateByScope(scope);
                })
                .catch(() => {
                  // apiCache may not be loaded yet during initial connection
                });
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('cache-invalidated', { detail: data.data }));
              }
              return;
            } else if (data.type === 'speaker_updated') {
              // Speaker attributes detected — dispatch event so components refresh
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('speaker-updated', { detail: data.data }));
              }
              return;
            } else if (data.type === 'speaker_processing_complete') {
              // Speaker background processing complete — silent notification, dispatch event only
              if (typeof window !== 'undefined') {
                window.dispatchEvent(
                  new CustomEvent('speaker-processing-complete', { detail: data.data })
                );
              }
              return;
            } else if (data.type === 'clustering_progress') {
              // Speaker clustering progress — dispatch event for SpeakersPage
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('clustering-progress', { detail: data.data }));
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'clustering_complete') {
              // Speaker clustering complete — dispatch event for SpeakersPage
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('clustering-complete', { detail: data.data }));
              }
              // Fall through to progressive notification handler
            } else if (data.type === 'clustering_file_complete') {
              // Per-file clustering complete — dispatch event for SpeakersPage
              if (typeof window !== 'undefined') {
                window.dispatchEvent(
                  new CustomEvent('clustering-file-complete', { detail: data.data })
                );
              }
              return;
            } else if (data.type === 'collection_shared') {
              // Collection shared with user — invalidate collections cache and notify
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('collection-shared', { detail: data.data }));
              }
              import('$lib/apiCache')
                .then(({ apiCache }) => {
                  apiCache.invalidateByScope('collections');
                  apiCache.invalidateByScope('files');
                  apiCache.invalidateByScope('shares');
                  apiCache.invalidateByScope('shared_collections');
                })
                .catch(() => {});
              // Fall through to create a visible notification
            } else if (data.type === 'collection_share_revoked') {
              // Collection share revoked — invalidate caches and notify
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('share-revoked', { detail: data.data }));
              }
              import('$lib/apiCache')
                .then(({ apiCache }) => {
                  apiCache.invalidateByScope('collections');
                  apiCache.invalidateByScope('files');
                  apiCache.invalidateByScope('shares');
                  apiCache.invalidateByScope('shared_collections');
                })
                .catch(() => {});
              // Fall through to create a visible notification
            } else if (data.type === 'collection_share_updated') {
              // Collection share permissions updated — invalidate caches and notify
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('share-updated', { detail: data.data }));
              }
              import('$lib/apiCache')
                .then(({ apiCache }) => {
                  apiCache.invalidateByScope('collections');
                  apiCache.invalidateByScope('shares');
                  apiCache.invalidateByScope('shared_collections');
                })
                .catch(() => {});
              // Fall through to create a visible notification
            } else if (data.type === 'group_member_added') {
              // User added to a group — invalidate caches and notify
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('group-member-added', { detail: data.data }));
              }
              import('$lib/apiCache')
                .then(({ apiCache }) => {
                  apiCache.invalidateByScope('collections');
                  apiCache.invalidateByScope('files');
                  apiCache.invalidateByScope('shares');
                  apiCache.invalidateByScope('shared_collections');
                })
                .catch(() => {});
              // Fall through to create a visible notification
            } else if (data.type === 'group_member_removed') {
              // User removed from a group — invalidate caches and notify
              if (typeof window !== 'undefined') {
                window.dispatchEvent(
                  new CustomEvent('group-member-removed', { detail: data.data })
                );
              }
              import('$lib/apiCache')
                .then(({ apiCache }) => {
                  apiCache.invalidateByScope('collections');
                  apiCache.invalidateByScope('files');
                  apiCache.invalidateByScope('shares');
                  apiCache.invalidateByScope('shared_collections');
                })
                .catch(() => {});
              // Fall through to create a visible notification
            } else if (data.type === 'file_takedown' || data.type === 'file_takedown_released') {
              // DMCA takedown / release owner notice — the file's visibility just
              // changed, so drop cached file lists before notifying
              import('$lib/apiCache')
                .then(({ apiCache }) => {
                  apiCache.invalidateByScope('files');
                })
                .catch(() => {});
              // Fall through to create a visible (persistent) notification
            }

            // Dispatch auto-label status events for AutoLabelSettings component
            if (data.type === 'auto_label_status') {
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('auto-label-status', { detail: data.data }));
              }
              // Fall through to progressive notification handler
            }

            // Dispatch document-parse status for the document detail page. Keyed on
            // `document_id`, not `file_id` (backend/app/tasks/document_tasks.py:_notify),
            // so it cannot be routed through the progressive-notification grouping below —
            // that machinery keys every progressId off `data.data.file_id` throughout this
            // file, and document_status genuinely doesn't have one. Same precedent as
            // auto-label-status above: a raw window CustomEvent, consumed directly by the
            // one page that needs it. Unlike that one, this RETURNS rather than falling
            // through: an unmatched type lands in the final `else` below and becomes a
            // persisted, visible notification-panel entry — exactly what the progressive
            // path exists to avoid, and parsing emits several progress ticks per document.
            if (data.type === 'document_status') {
              if (typeof window !== 'undefined') {
                window.dispatchEvent(new CustomEvent('document-status', { detail: data.data }));
              }
              return;
            }

            // Handle progressive notifications
            const isProgressiveType =
              data.type === 'transcription_status' ||
              data.type === 'summarization_status' ||
              data.type === 'redaction_status' ||
              data.type === 'topic_extraction_status' ||
              data.type === 'auto_label_status' ||
              data.type === 'youtube_processing_status' ||
              data.type === 'playlist_processing_status';

            // Admin task types that should also appear in NotificationsPanel
            const isAdminProgressType = data.type in ADMIN_TASK_PROGRESS_IDS;

            // Handle silent notifications (gallery-only updates, internal state events)
            const isSilentType =
              data.type === 'file_created' ||
              data.type === 'file_updated' ||
              data.type === 'search_indexing_complete' ||
              data.type === 'clustering_file_complete';

            const isEnrichmentType =
              data.type === 'enrichment_started' || data.type === 'enrichment_task_complete';

            if (
              (isProgressiveType &&
                (data.data.file_id || data.type === 'playlist_processing_status')) ||
              isAdminProgressType
            ) {
              // Determine progressId
              let progressId: string;
              if (isAdminProgressType) {
                progressId = ADMIN_TASK_PROGRESS_IDS[data.type];
              } else if (data.type === 'playlist_processing_status') {
                progressId = `playlist_processing_${data.data.playlist_id || 'unknown'}`;
              } else {
                progressId = `${data.type}_${data.data.file_id}`;
              }

              // Determine status — admin completion types map to 'completed'
              let status: string;
              if (ADMIN_COMPLETION_TYPES.has(data.type)) {
                status = 'completed';
              } else if (isAdminProgressType) {
                status = 'processing';
              } else {
                status = data.data.status || 'processing';
              }

              // Synthesize message for admin tasks that don't have one
              const translate = get(t);
              let currentStep = data.data.message || '';
              if (!currentStep && isAdminProgressType) {
                if (data.type.startsWith('reindex')) {
                  const idx = data.data.indexed_files ?? data.data.stats?.indexed_files ?? 0;
                  const tot = data.data.total_files ?? data.data.stats?.total_files ?? 0;
                  currentStep =
                    status === 'completed'
                      ? translate('notifications.progress.indexedComplete', { count: idx })
                      : translate('notifications.progress.indexedProgress', {
                          count: idx,
                          total: tot,
                        });
                } else if (data.type.startsWith('migration')) {
                  const proc = data.data.processed_files ?? 0;
                  const tot = data.data.total_files ?? 0;
                  currentStep =
                    status === 'completed'
                      ? translate('notifications.progress.processedComplete', { count: proc })
                      : translate('notifications.progress.processedProgress', {
                          count: proc,
                          total: tot,
                        });
                } else if (data.type.startsWith('clustering')) {
                  const step = data.data.step ?? 0;
                  const tot = data.data.total_steps ?? 0;
                  currentStep =
                    status === 'completed'
                      ? translate('notifications.progress.clusteringComplete')
                      : translate('notifications.progress.clusteringStep', { step, total: tot });
                } else if (data.type.startsWith('attribute_migration')) {
                  const proc = data.data.processed_files ?? 0;
                  const tot = data.data.total_files ?? 0;
                  currentStep =
                    status === 'completed'
                      ? translate('notifications.progress.processedComplete', { count: proc })
                      : translate('notifications.progress.processedProgress', {
                          count: proc,
                          total: tot,
                        });
                } else if (data.type.startsWith('data_integrity')) {
                  const proc = data.data.processed_files ?? data.data.processed ?? 0;
                  const tot = data.data.total_files ?? data.data.total ?? 0;
                  currentStep =
                    status === 'completed'
                      ? translate('notifications.progress.checkedComplete', { count: proc })
                      : translate('notifications.progress.checkedProgress', {
                          count: proc,
                          total: tot,
                        });
                } else if (data.type.startsWith('embedding_consistency')) {
                  const proc = data.data.processed_files ?? 0;
                  const tot = data.data.total_files ?? 0;
                  currentStep =
                    status === 'completed'
                      ? translate('notifications.progress.repairedComplete', {
                          count: data.data.repaired ?? 0,
                        })
                      : translate('notifications.progress.repairingProgress', {
                          count: proc,
                          total: tot,
                        });
                }
              }
              if (!currentStep) currentStep = translate('notifications.progress.processing');

              // Normalize progress: admin tasks send 0.0-1.0, others send 0-100
              let rawProgress = data.data.progress || 0;
              if (isAdminProgressType && rawProgress <= 1 && rawProgress > 0) {
                rawProgress = rawProgress * 100;
              }

              // Read ETA from data
              const etaRaw = data.data.eta_seconds ?? null;
              const etaDisplay = formatEtaSeconds(etaRaw);

              const progress = {
                current: Math.floor(rawProgress),
                total: 100,
                percentage: Math.round(rawProgress),
                etaSeconds: etaRaw,
                etaDisplay,
              };

              update((s: WebSocketState) => {
                // Find existing progressive notification
                const existingIndex = s.notifications.findIndex((n) => n.progressId === progressId);

                if (existingIndex !== -1) {
                  // Update existing notification with new data (important for summary completion)
                  const updatedNotification = {
                    ...s.notifications[existingIndex],
                    message: currentStep,
                    timestamp: new Date(),
                    currentStep,
                    progress,
                    status: status as 'processing' | 'completed' | 'error',
                    dismissible:
                      status === 'completed' ||
                      status === 'failed' ||
                      status === 'error' ||
                      status === 'not_configured' ||
                      (data.type === 'youtube_processing_status' && status === 'pending'),
                    read: false, // Mark as unread for updates
                    data: {
                      ...s.notifications[existingIndex].data,
                      ...data.data,
                    }, // Merge old and new data
                  };

                  // Remove from current position and add to front to maintain most-recent-first ordering
                  s.notifications.splice(existingIndex, 1);
                  s.notifications.unshift(updatedNotification);
                } else {
                  // Create new progressive notification
                  const notification: Notification = {
                    id: generateId('ws'),
                    progressId,
                    type: data.type as NotificationType,
                    title: getNotificationTitle(data.type),
                    message: currentStep,
                    timestamp: new Date(),
                    read: false,
                    data: data.data,
                    currentStep,
                    progress,
                    status: status as 'processing' | 'completed' | 'error',
                    dismissible:
                      status === 'completed' ||
                      status === 'failed' ||
                      status === 'error' ||
                      (data.type === 'youtube_processing_status' && status === 'pending'),
                  };

                  s.notifications = [notification, ...s.notifications.slice(0, 99)];
                }

                saveNotificationsToStorage(s.notifications);
                return s;
              });
            } else if (isEnrichmentType) {
              // Enrichment events update the parent transcription notification with chips
              const fileId = data.data.file_id;
              const progressId = `transcription_status_${fileId}`;

              update((s: WebSocketState) => {
                const parentIndex = s.notifications.findIndex((n) => n.progressId === progressId);

                if (parentIndex !== -1) {
                  const parent = s.notifications[parentIndex];

                  if (data.type === 'enrichment_started') {
                    // Store expected task list
                    parent.enrichmentTasks = data.data.tasks || [];
                    parent.completedEnrichments = parent.completedEnrichments || [];
                  } else if (data.type === 'enrichment_task_complete') {
                    // Add completed task chip (avoid duplicates)
                    const taskName = data.data.task;
                    if (!parent.completedEnrichments) {
                      parent.completedEnrichments = [];
                    }
                    if (!parent.completedEnrichments.includes(taskName)) {
                      parent.completedEnrichments = [...parent.completedEnrichments, taskName];
                    }
                  }

                  // Trigger reactivity
                  s.notifications[parentIndex] = { ...parent };
                  saveNotificationsToStorage(s.notifications);
                }
                return s;
              });
            } else if (isSilentType) {
              // Create a silent notification for gallery updates only
              const notification: Notification = {
                id: generateId('ws'),
                type: data.type as NotificationType,
                title: getNotificationTitle(data.type),
                message: data.data.message || get(t)('notifications.galleryUpdate'),
                timestamp: new Date(),
                read: false,
                data: data.data,
                dismissible: true,
                silent: true, // This prevents it from showing in notification panel
              };

              // Add notification (MediaLibrary will see it, but NotificationsPanel will filter it out)
              update((s: WebSocketState) => {
                s.notifications = [notification, ...s.notifications.slice(0, 99)];
                // Don't save silent notifications to localStorage to keep it clean
                return s;
              });
            } else {
              // Create a regular notification for other non-progressive types
              const notification: Notification = {
                id: generateId('ws'),
                type: data.type as NotificationType,
                title: getNotificationTitle(data.type),
                message:
                  composeClientMessage(data.type, data.data) ||
                  data.data.message ||
                  get(t)('notifications.noMessage'),
                timestamp: new Date(),
                read: false,
                data: data.data,
                dismissible: true,
              };

              // Add notification
              update((s: WebSocketState) => {
                s.notifications = [notification, ...s.notifications.slice(0, 99)]; // Keep max 100 notifications
                saveNotificationsToStorage(s.notifications);
                return s;
              });
            }
          } catch (error) {
            console.error('Error processing WebSocket message:', error);
          }
        };
      } catch (error) {
        state.status = 'error';
        state.error = 'Failed to connect to WebSocket server';
        console.error('WebSocket connection error:', error);
      }

      return state;
    });
  };

  // Try to reconnect with jittered exponential backoff.
  //
  // The jitter matters: a backend restart drops every connected client at the
  // same instant, and an un-jittered 2/4/8/16/30 s grid makes all of them
  // retry in lockstep, so the server takes a synchronised burst on every tick
  // while it is still coming up. `reconnectDelayMs` spreads each client over
  // the second half of its backoff window instead.
  const tryReconnect = () => {
    update((state: WebSocketState) => {
      state.reconnectAttempts += 1;
      return state;
    });

    reconnectTimeout = setTimeout(() => {
      connect();
    }, reconnectDelayMs(getState().reconnectAttempts));
  };

  // Disconnect
  const disconnect = () => {
    update((state: WebSocketState) => {
      if (state.socket) {
        state.socket.close(1000, 'User logged out');
        state.socket = null;
      }

      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }

      state.status = 'disconnected';
      return state;
    });
  };

  // Send message
  const send = (message: unknown) => {
    update((state: WebSocketState) => {
      if (state.socket && state.status === 'connected') {
        state.socket.send(JSON.stringify(message));
      } else {
        console.warn('Cannot send message, WebSocket not connected');
      }
      return state;
    });
  };

  // Mark notification as read (with auto-regeneration for processing notifications)
  const markAsRead = (id: string) => {
    update((state: WebSocketState) => {
      const index = state.notifications.findIndex((n: Notification) => n.id === id);
      if (index !== -1) {
        const notification = state.notifications[index];

        // If it's a processing notification, auto-regenerate it
        if (notification.status === 'processing' && !notification.dismissible) {
          // Mark as read but keep the notification
          state.notifications[index].read = true;
          // Re-add as unread after a short delay (simulated by creating a duplicate)
          setTimeout(() => {
            update((s: WebSocketState) => {
              const stillExists = s.notifications.find(
                (n) => n.id === id && n.status === 'processing'
              );
              if (stillExists) {
                stillExists.read = false;
                saveNotificationsToStorage(s.notifications);
              }
              return s;
            });
          }, 100);
        } else {
          // Regular dismissal for completed/error notifications
          state.notifications[index].read = true;
        }

        saveNotificationsToStorage(state.notifications);
      }
      return state;
    });
  };

  // Mark all notifications as read
  const markAllAsRead = () => {
    update((state: WebSocketState) => {
      state.notifications = state.notifications.map((n: Notification) => ({
        ...n,
        read: true,
      }));
      saveNotificationsToStorage(state.notifications);
      return state;
    });
  };

  // Clear all notifications
  const clearAll = () => {
    update((state: WebSocketState) => {
      state.notifications = [];
      saveNotificationsToStorage(state.notifications);
      return state;
    });
  };

  // Handle download progress messages (legacy/global). Media downloads initiated
  // from the file page are delivered over a dedicated SSE stream — see
  // TranscriptDisplay's EventSource — so this only mirrors status into the store.
  const handleDownloadProgress = (data: {
    data: {
      file_id?: string;
      status: DownloadState['status'];
      progress?: number;
      error?: string;
    };
  }) => {
    const { file_id, status, progress, error } = data.data;

    if (file_id) {
      downloadStore.updateStatus(file_id, status, progress, error);
    }
  };

  // Get a suitable title based on notification type
  const getNotificationTitle = (type: string): string => {
    const translate = get(t);
    switch (type) {
      case 'transcription_status':
        return translate('notifications.transcriptionUpdate');
      case 'summarization_status':
        return translate('notifications.summarizationUpdate');
      case 'redaction_status':
        return translate('notifications.redactionUpdate');
      case 'topic_extraction_status':
        return translate('notifications.topicExtraction');
      case 'auto_label_status':
        return translate('notifications.autoLabeling');
      case 'youtube_processing_status':
        return translate('notifications.youtubeProcessing');
      case 'playlist_processing_status':
        return translate('notifications.playlistProcessing');
      case 'analytics_status':
        return translate('notifications.analyticsUpdate');
      case 'backup_status':
        return translate('notifications.backupUpdate');
      case 'media_mirror_status':
        return translate('notifications.mediaMirrorUpdate');
      case 'download_progress':
        return translate('notifications.downloadProgress');
      case 'audio_extraction_status':
        return translate('notifications.audioExtraction');
      case 'file_upload':
        return translate('notifications.fileUpload');
      case 'file_created':
        return translate('notifications.fileCreated');
      case 'file_updated':
        return translate('notifications.fileUpdated');
      case 'file_deleted':
        return translate('notifications.fileDeleted');
      case 'file_takedown':
        return translate('notifications.fileTakedown');
      case 'file_takedown_released':
        return translate('notifications.fileTakedownReleased');
      case 'collection_shared':
        return translate('notifications.collectionShared');
      case 'collection_share_revoked':
        return translate('notifications.collectionShareRevoked');
      case 'collection_share_updated':
        return translate('notifications.collectionShareUpdated');
      case 'group_member_added':
        return translate('notifications.groupMemberAdded');
      case 'group_member_removed':
        return translate('notifications.groupMemberRemoved');
      case 'reindex_progress':
      case 'reindex_complete':
      case 'reindex_stopped':
        return translate('notifications.searchReindexing');
      case 'migration_progress':
      case 'migration_complete':
      case 'migration_finalized':
        return translate('notifications.embeddingMigration');
      case 'attribute_migration_progress':
      case 'attribute_migration_complete':
        return translate('notifications.speakerAttributeMigration');
      case 'clustering_progress':
      case 'clustering_complete':
        return translate('notifications.speakerClustering');
      case 'data_integrity_progress':
      case 'data_integrity_complete':
        return translate('notifications.dataIntegrity');
      case 'embedding_consistency_progress':
      case 'embedding_consistency_complete':
        return translate('notifications.embeddingConsistency');
      default:
        return translate('notifications.notification');
    }
  };

  // Compose a localized message for types rendered client-side from payload
  // fields (instead of the backend's English fallback message). Currently the
  // DMCA takedown / release owner notices (§512(g)): the quarantined file 404s
  // for its owner, so this notification is their only takedown surface and must
  // carry the reason + counter-notice contact in the user's language.
  const composeClientMessage = (type: string, data: NotificationData): string | undefined => {
    if (type !== 'file_takedown' && type !== 'file_takedown_released') return undefined;
    const translate = get(t);
    const filename = data.filename || '';
    if (type === 'file_takedown_released') {
      return translate('notifications.fileTakedownReleasedMessage', { filename });
    }
    const fileId = data.file_uuid || '';
    const taken = translate('notifications.fileTakedownMessage', {
      filename,
      reason: data.reason || '',
    });
    const dispute = data.abuse_contact_email
      ? translate('notifications.fileTakedownCounterNotice', {
          email: data.abuse_contact_email,
          fileId,
        })
      : translate('notifications.fileTakedownCounterNoticeNoEmail', { fileId });
    return `${taken} ${dispute}`;
  };

  // Add a notification manually (for client-side events like audio extraction)
  const addNotification = (notification: Omit<Notification, 'id' | 'timestamp' | 'read'>) => {
    update((state: WebSocketState) => {
      const newNotification: Notification = {
        ...notification,
        id: generateId('ws'),
        timestamp: new Date(),
        read: false,
      };

      // If this is a progressive notification, check if we should update existing one
      if (notification.progressId) {
        const existingIndex = state.notifications.findIndex(
          (n) => n.progressId === notification.progressId
        );
        if (existingIndex !== -1) {
          // Update existing notification
          state.notifications[existingIndex] = {
            ...state.notifications[existingIndex],
            ...newNotification,
            id: state.notifications[existingIndex].id, // Keep original ID
            timestamp: state.notifications[existingIndex].timestamp, // Keep original timestamp
          };
        } else {
          // Add new notification at the beginning
          state.notifications = [newNotification, ...state.notifications];
        }
      } else {
        // Add new notification at the beginning
        state.notifications = [newNotification, ...state.notifications];
      }

      saveNotificationsToStorage(state.notifications);
      return state;
    });
  };

  // Update an existing notification (for progressive updates)
  const updateNotification = (progressId: string, updates: Partial<Notification>) => {
    update((state: WebSocketState) => {
      const existingIndex = state.notifications.findIndex((n) => n.progressId === progressId);
      if (existingIndex !== -1) {
        state.notifications[existingIndex] = {
          ...state.notifications[existingIndex],
          ...updates,
        };
        saveNotificationsToStorage(state.notifications);
      }
      return state;
    });
  };

  // Remove notification (with auto-regeneration for processing notifications)
  const removeNotification = (id: string) => {
    update((state: WebSocketState) => {
      const notification = state.notifications.find((n) => n.id === id);

      if (notification && notification.status === 'processing' && !notification.dismissible) {
        // Auto-regenerate processing notifications
        setTimeout(() => {
          update((s: WebSocketState) => {
            // Only re-add if it doesn't already exist
            const exists = s.notifications.find(
              (n) => n.progressId === notification.progressId && n.status === 'processing'
            );
            if (!exists) {
              const regenerated: Notification = {
                ...notification,
                id: generateId('ws'),
                timestamp: new Date(),
                read: false,
              };
              s.notifications = [regenerated, ...s.notifications];
              saveNotificationsToStorage(s.notifications);
            }
            return s;
          });
        }, 100);
      }

      // Remove the notification
      state.notifications = state.notifications.filter((n) => n.id !== id);
      saveNotificationsToStorage(state.notifications);
      return state;
    });
  };

  // Recover active task progress from backend on connect/reconnect
  const recoverActiveProgress = async () => {
    try {
      const { data: activeTasks } = await axiosInstance.get<
        Array<{
          task_type: string;
          user_id: number;
          total: number;
          processed: number;
          status: string;
          message: string;
          eta_seconds: number | null;
        }>
      >('/tasks/progress/active');

      if (!activeTasks || activeTasks.length === 0) return;

      // Task type to notification type mapping
      const typeMap: Record<string, string> = {
        reindex: 'reindex_progress',
        migration: 'migration_progress',
        attribute_migration: 'attribute_migration_progress',
        clustering: 'clustering_progress',
        auto_label: 'auto_label_status',
        data_integrity: 'data_integrity_progress',
        embedding_consistency: 'embedding_consistency_progress',
      };

      for (const task of activeTasks) {
        const notificationType = typeMap[task.task_type];
        if (!notificationType) continue;

        const progressId =
          ADMIN_TASK_PROGRESS_IDS[notificationType] || `${notificationType}_retroactive_apply`;

        const percentage = task.total > 0 ? Math.round((task.processed / task.total) * 100) : 0;
        const etaDisplay = formatEtaSeconds(task.eta_seconds);

        // Dispatch CustomEvent for settings components
        if (typeof window !== 'undefined') {
          const eventMap: Record<string, string> = {
            reindex: 'reindex-progress',
            migration: 'migration-progress',
            attribute_migration: 'attribute-migration-progress',
            clustering: 'clustering-progress',
            auto_label: 'auto-label-status',
            data_integrity: 'data-integrity-progress',
            embedding_consistency: 'embedding-consistency-progress',
          };
          const eventName = eventMap[task.task_type];
          if (eventName) {
            window.dispatchEvent(
              new CustomEvent(eventName, {
                detail: {
                  progress: task.total > 0 ? task.processed / task.total : 0,
                  processed: task.processed,
                  total: task.total,
                  processed_files: task.processed,
                  total_files: task.total,
                  indexed_files: task.processed,
                  message: task.message,
                  eta_seconds: task.eta_seconds,
                  running: true,
                  status: 'processing',
                },
              })
            );
          }
        }

        // Create/update progressive notification
        update((s: WebSocketState) => {
          const existingIndex = s.notifications.findIndex((n) => n.progressId === progressId);

          const notificationData: Notification = {
            id: existingIndex !== -1 ? s.notifications[existingIndex].id : generateId('ws'),
            progressId,
            type: notificationType as NotificationType,
            title: getNotificationTitle(notificationType),
            message: task.message,
            timestamp: new Date(),
            read: false,
            data: {
              progress: percentage,
              processed: task.processed,
              total: task.total,
              eta_seconds: task.eta_seconds,
            },
            currentStep: task.message,
            progress: {
              current: percentage,
              total: 100,
              percentage,
              etaSeconds: task.eta_seconds,
              etaDisplay,
            },
            status: 'processing',
            dismissible: false,
          };

          if (existingIndex !== -1) {
            s.notifications[existingIndex] = notificationData;
          } else {
            s.notifications = [notificationData, ...s.notifications.slice(0, 99)];
          }

          saveNotificationsToStorage(s.notifications);
          return s;
        });
      }
    } catch {
      // Recovery is best-effort; don't break WebSocket on failure
    }
  };

  return {
    subscribe,
    connect,
    disconnect,
    send,
    markAsRead,
    markAllAsRead,
    clearAll,
    removeNotification,
    addNotification,
    updateNotification,
  };
}

// Create the WebSocket store
export const websocketStore = createWebSocketStore();

// Derived store for unread notifications count (excluding silent notifications)
export const unreadCount = derived(
  websocketStore,
  ($websocketStore: WebSocketState) =>
    $websocketStore.notifications.filter((n: Notification) => !n.read && !n.silent).length
);

/**
 * Simplified, UI-facing connection status.
 *
 * Collapses the store's internal {@link ConnectionStatus} into the three states the UI
 * actually cares about for surfacing connection loss:
 *  - `connected`     — socket open, live updates flowing.
 *  - `reconnecting`  — connecting, or recovering after a non-clean close (backoff retries).
 *  - `disconnected`  — closed/errored with no reconnect in progress.
 *
 * Purely observational — it does not drive the reconnection logic, it only reflects it.
 */
export type UiConnectionStatus = 'connected' | 'reconnecting' | 'disconnected';

export const connectionStatus = derived<typeof websocketStore, UiConnectionStatus>(
  websocketStore,
  ($ws) => {
    if ($ws.status === 'connected') return 'connected';
    if ($ws.status === 'connecting' || $ws.reconnectAttempts > 0) return 'reconnecting';
    return 'disconnected';
  }
);

// Initialize WebSocket when auth changes (cookies sent automatically)
authStore.token.subscribe((token: string | null) => {
  if (token) {
    websocketStore.connect();
  } else {
    websocketStore.disconnect();
  }
});

// Handle page visibility changes to reconnect when page becomes visible
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      // Page became visible, check if we need to reconnect
      let shouldReconnect = false;

      const unsubscribe = websocketStore.subscribe((state: WebSocketState) => {
        if (state.status === 'disconnected' || state.status === 'error') {
          // Check if we're still authenticated (token is in httpOnly cookie)
          if (get(authStore.isAuthenticated)) {
            shouldReconnect = true;
          }
        }
      });
      unsubscribe();

      if (shouldReconnect) {
        websocketStore.connect();
      }
    }
  });
}
