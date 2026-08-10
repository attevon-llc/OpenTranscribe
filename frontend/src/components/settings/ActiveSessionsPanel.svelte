<script lang="ts">
  import { onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { logout } from '$stores/auth';
  import { toastStore } from '$stores/toast';
  import { t, locale } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { formatDate } from '$lib/utils/formatting';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';

  /**
   * One row of `GET /api/auth/sessions` — an active (non-revoked, unexpired)
   * refresh token. `is_current` is optional because the API does not send it
   * today: auth is httpOnly-cookie based, so the browser cannot compare its own
   * token against a `jti` either. The badge appears only if the backend starts
   * marking the row.
   */
  interface ActiveSession {
    jti: string;
    created_at: string;
    expires_at: string;
    user_agent: string | null;
    ip_address: string | null;
    is_current?: boolean;
  }

  let sessions: ActiveSession[] = [];
  /** The API's own count — rendered rather than recomputed from the array. */
  let total = 0;
  let loading = true;
  let signingOut = false;
  let showSignOutConfirm = false;

  onMount(loadSessions);

  async function loadSessions() {
    loading = true;
    try {
      const response = await axiosInstance.get('/auth/sessions');
      sessions = response.data?.sessions ?? [];
      total = response.data?.total ?? sessions.length;
    } catch (err: unknown) {
      console.error('Error loading active sessions:', err);
      toastStore.error(getErrorMessage(err, $t('settings.sessions.loadFailed')));
      sessions = [];
      total = 0;
    } finally {
      loading = false;
    }
  }

  /**
   * `POST /auth/logout/all` revokes every refresh token AND clears this browser's
   * auth cookies, so the caller is signed out too. Tearing the client down
   * through the store's `logout()` keeps session cleanup in its single home
   * (`$lib/session/clearUserState`) — its own `POST /auth/logout` is a harmless
   * no-op against the already-cleared cookies.
   *
   * The redirect is a full document load rather than a client-side `goto`: every
   * session this tab was built on is gone, so discarding the whole JS heap is
   * the strongest guarantee that nothing from it survives into the login page.
   */
  async function signOutEverywhere() {
    signingOut = true;
    try {
      await axiosInstance.post('/auth/logout/all');
      await logout();
      window.location.assign('/login');
    } catch (err: unknown) {
      console.error('Error signing out of all sessions:', err);
      toastStore.error(getErrorMessage(err, $t('settings.sessions.signOutFailed')));
      signingOut = false;
    }
  }

  /**
   * Coarse browser/OS label for a user-agent string. Deliberately shallow — this
   * is a hint about "which device is this", not UA sniffing, and an unrecognised
   * agent falls back to the raw string rather than claiming something wrong.
   */
  function describeUserAgent(ua: string | null): string {
    if (!ua) return $t('settings.sessions.unknownDevice');
    const browser =
      /Edg\//.test(ua) ? 'Edge'
      : /OPR\/|Opera/.test(ua) ? 'Opera'
      : /Chrome\//.test(ua) ? 'Chrome'
      : /Firefox\//.test(ua) ? 'Firefox'
      : /Safari\//.test(ua) ? 'Safari'
      : null;
    const platform =
      /Windows/.test(ua) ? 'Windows'
      : /Android/.test(ua) ? 'Android'
      : /iPhone|iPad|iOS/.test(ua) ? 'iOS'
      : /Mac OS X|Macintosh/.test(ua) ? 'macOS'
      : /Linux/.test(ua) ? 'Linux'
      : null;
    if (browser && platform) return `${browser} — ${platform}`;
    return browser ?? platform ?? ua;
  }

  const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
    ['year', 365 * 24 * 3600],
    ['month', 30 * 24 * 3600],
    ['day', 24 * 3600],
    ['hour', 3600],
    ['minute', 60],
    ['second', 1],
  ];

  /**
   * Locale-aware "3 hours ago" / "in 6 days" via `Intl.RelativeTimeFormat`.
   *
   * `$lib/utils/formatting` is the single home for time formatting but has no
   * relative helper, and `src/lib/utils/CLAUDE.md` says relative labels
   * deliberately do not belong there — so this leans on the platform formatter
   * rather than hand-rolling (or adding) one. The absolute timestamp stays
   * available as the cell's `title`.
   */
  function formatRelative(iso: string, currentLocale: string): string {
    const target = new Date(iso).getTime();
    if (Number.isNaN(target)) return iso;
    const deltaSeconds = (target - Date.now()) / 1000;
    const rtf = new Intl.RelativeTimeFormat(currentLocale || 'en', { numeric: 'auto' });
    for (const [unit, seconds] of RELATIVE_UNITS) {
      if (Math.abs(deltaSeconds) >= seconds || unit === 'second') {
        return rtf.format(Math.round(deltaSeconds / seconds), unit);
      }
    }
    return iso;
  }

  function absoluteTime(iso: string): string {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return `${formatDate(iso)} ${date.toLocaleTimeString($locale || 'en')}`;
  }
</script>

<div class="sessions-panel">
  <div class="panel-header">
    <div>
      <h4 class="card-title">{$t('settings.sessions.title')}</h4>
      <p class="panel-description">{$t('settings.sessions.description')}</p>
    </div>
    <button
      type="button"
      class="btn btn-secondary"
      on:click={loadSessions}
      disabled={loading || signingOut}
    >
      {$t('settings.sessions.refresh')}
    </button>
  </div>

  {#if loading}
    <p class="panel-status">{$t('settings.sessions.loading')}</p>
  {:else if sessions.length === 0}
    <EmptyState
      title={$t('settings.sessions.empty')}
      description={$t('settings.sessions.emptyDescription')}
      padding="1.5rem"
    />
  {:else}
    <p class="panel-status">{$t('settings.sessions.count', { total })}</p>
    <div class="table-scroll">
      <table class="sessions-table">
        <thead>
          <tr>
            <th>{$t('settings.sessions.device')}</th>
            <th>{$t('settings.sessions.ipAddress')}</th>
            <th>{$t('settings.sessions.started')}</th>
            <th>{$t('settings.sessions.expires')}</th>
          </tr>
        </thead>
        <tbody>
          {#each sessions as session (session.jti)}
            <tr>
              <td>
                <span class="device">{describeUserAgent(session.user_agent)}</span>
                {#if session.is_current}
                  <span class="current-badge">{$t('settings.sessions.current')}</span>
                {/if}
              </td>
              <td class="mono">{session.ip_address || $t('settings.sessions.unknownIp')}</td>
              <td title={absoluteTime(session.created_at)}>
                {formatRelative(session.created_at, $locale)}
              </td>
              <td title={absoluteTime(session.expires_at)}>
                {formatRelative(session.expires_at, $locale)}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <p class="panel-note">{$t('settings.sessions.readOnlyNote')}</p>

  <div class="panel-actions">
    <button
      type="button"
      class="btn btn-danger"
      on:click={() => (showSignOutConfirm = true)}
      disabled={signingOut || loading || sessions.length === 0}
    >
      {signingOut ? $t('settings.sessions.signingOut') : $t('settings.sessions.signOutEverywhere')}
    </button>
  </div>
</div>

<ConfirmationModal
  bind:isOpen={showSignOutConfirm}
  title={$t('settings.sessions.signOutEverywhere')}
  message={$t('settings.sessions.signOutConfirmMessage')}
  confirmText={$t('settings.sessions.signOutEverywhere')}
  cancelText={$t('common.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={signOutEverywhere}
/>

<style>
  .sessions-panel {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .panel-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }

  .card-title {
    font-size: 0.9rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
    color: var(--text-color);
  }

  .panel-description,
  .panel-status,
  .panel-note {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin: 0;
  }

  .panel-note {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-color);
  }

  .table-scroll {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }

  .sessions-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.75rem;
  }

  .sessions-table th,
  .sessions-table td {
    padding: 0.375rem 0.5rem;
    text-align: left;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
    white-space: nowrap;
  }

  .sessions-table th {
    font-weight: 600;
    font-size: 0.6875rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    color: var(--text-secondary);
  }

  .sessions-table tbody tr:hover {
    background-color: var(--table-row-hover);
  }

  .device {
    color: var(--text-color);
  }

  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    color: var(--text-secondary);
  }

  .current-badge {
    margin-left: 0.375rem;
    padding: 0.0625rem 0.375rem;
    border-radius: 999px;
    font-size: 0.625rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    background-color: rgba(59, 130, 246, 0.15);
    color: var(--primary-color, #3b82f6);
  }

  .panel-actions {
    display: flex;
    justify-content: flex-end;
  }

  .btn {
    padding: 0.5rem 1rem;
    border-radius: 10px;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn:disabled {
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
  }

  .btn-danger {
    background-color: rgba(239, 68, 68, 0.12);
    color: #dc2626;
    border: 1px solid rgba(239, 68, 68, 0.4);
  }

  .btn-danger:hover:not(:disabled) {
    background-color: #ef4444;
    color: white;
  }

  :global([data-theme='dark']) .btn-danger {
    color: #f87171;
  }

  :global([data-theme='dark']) .btn-danger:hover:not(:disabled) {
    color: white;
  }

  @media (max-width: 768px) {
    .panel-header {
      flex-direction: column;
    }

    .panel-actions .btn,
    .panel-header .btn {
      width: 100%;
      min-height: 44px;
    }
  }
</style>
