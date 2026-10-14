<script lang="ts">
  /**
   * Condensed File Status, the Settings face of `/file-status`.
   *
   * Counts, the files that need attention and the last day's uploads, from the same
   * `GET /my-files/status` the full page reads (same cache key, so opening either warms the
   * other). The per-task grid, filters and detail modal stay on the full page behind
   * "Open full screen": one canonical view, no second copy of it.
   */
  import { onMount, onDestroy } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { t } from '$stores/locale';
  import Badge from '$components/ui/Badge.svelte';
  import Spinner from '$components/ui/Spinner.svelte';

  interface StatusFile {
    uuid: string;
    filename: string;
    status: string | null;
    display_status: string;
    formatted_file_age: string;
  }
  interface StatusPayload {
    status_counts: Record<'total' | 'completed' | 'processing' | 'pending' | 'error', number>;
    problem_files: { count: number; files: StatusFile[] };
    recent_files: { count: number; files: StatusFile[] };
    has_problems: boolean;
  }

  const POLL_MS = 30_000;
  const COUNTS = [
    { key: 'total', label: 'fileStatus.totalFiles' },
    { key: 'completed', label: 'common.completed' },
    { key: 'processing', label: 'common.processing' },
    { key: 'pending', label: 'common.pending' },
    { key: 'error', label: 'fileStatus.errors' },
  ] as const;

  let data: StatusPayload | null = null;
  let loading = true;
  let error: string | null = null;
  let timer: ReturnType<typeof setInterval> | null = null;

  function variantFor(status: string | null): 'success' | 'warning' | 'error' | 'info' | 'default' {
    switch (status) {
      case 'completed':
        return 'success';
      case 'error':
        return 'error';
      case 'processing':
        return 'info';
      case 'pending':
        return 'warning';
      default:
        return 'default';
    }
  }

  async function load(silent = false) {
    if (!silent) loading = true;
    try {
      if (silent) apiCache.invalidate('status:');
      data = await apiCache.getOrFetch(
        cacheKey.status(),
        async () => (await axiosInstance.get('/my-files/status')).data,
        CacheTTL.STATUS
      );
      error = null;
    } catch (err: unknown) {
      if (!silent) error = getErrorMessage(err, $t('fileStatus.loadFailed'));
    } finally {
      loading = false;
    }
  }

  const onInvalidated = (event: Event) => {
    const scope = (event as CustomEvent).detail?.scope;
    if (scope === 'files' || scope === 'all') void load(true);
  };

  onMount(() => {
    void load();
    timer = setInterval(() => void load(true), POLL_MS);
    window.addEventListener('cache-invalidated', onInvalidated);
  });

  onDestroy(() => {
    if (timer) clearInterval(timer);
    if (typeof window !== 'undefined') window.removeEventListener('cache-invalidated', onInvalidated);
  });
</script>

<div class="content-section">
  <div class="section-header-row">
    <div>
      <h3 class="section-title">{$t('settings.fileStatus.title')}</h3>
      <p class="section-description">{$t('settings.fileStatus.description')}</p>
    </div>
    <a
      href="/file-status"
      class="btn btn-secondary open-full"
      data-testid="file-status-open-full"
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="15 3 21 3 21 9"></polyline>
        <polyline points="9 21 3 21 3 15"></polyline>
        <line x1="21" y1="3" x2="14" y2="10"></line>
        <line x1="3" y1="21" x2="10" y2="14"></line>
      </svg>
      {$t('settings.fileStatus.openFullScreen')}
    </a>
  </div>

  {#if loading && !data}
    <div class="loading-state"><Spinner size="large" /></div>
  {:else if error}
    <div class="error-message" role="alert">{error}</div>
  {:else if data}
    <div class="counts">
      {#each COUNTS as c}
        <div class="count-card" class:error={c.key === 'error' && data.status_counts.error > 0}>
          <div class="count-number">{data.status_counts[c.key]}</div>
          <div class="count-label">{$t(c.label)}</div>
        </div>
      {/each}
    </div>

    {#if data.has_problems}
      <h4 class="list-title">{$t('fileStatus.filesNeedAttention')}</h4>
      <ul class="file-list">
        {#each data.problem_files.files as f (f.uuid)}
          <li>
            <a class="file-name" href={`/files/${f.uuid}`}>{f.filename}</a>
            <span class="file-age">{f.formatted_file_age}</span>
            <Badge variant={variantFor(f.status)}>{f.display_status}</Badge>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="all-normal">{$t('fileStatus.allFilesNormal')}</p>
    {/if}

    <h4 class="list-title">{$t('fileStatus.recentFiles')}</h4>
    {#if data.recent_files.files.length}
      <ul class="file-list">
        {#each data.recent_files.files as f (f.uuid)}
          <li>
            <a class="file-name" href={`/files/${f.uuid}`}>{f.filename}</a>
            <span class="file-age">{f.formatted_file_age}</span>
            <Badge variant={variantFor(f.status)}>{f.display_status}</Badge>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="empty">{$t('settings.fileStatus.noRecent')}</p>
    {/if}
  {/if}
</div>

<style>
  .section-header-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    margin-bottom: 1rem;
    padding-inline-end: 2rem;
  }

  .section-header-row .section-description {
    margin-bottom: 0;
  }

  .open-full {
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    white-space: nowrap;
  }

  .loading-state {
    display: flex;
    justify-content: center;
    padding: 2rem 0;
  }

  .counts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 0.75rem;
    margin-bottom: 1.5rem;
  }

  .count-card {
    padding: 0.75rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--background-color);
  }

  .count-number {
    font-size: 1.5rem;
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .count-label {
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .count-card.error .count-number {
    color: var(--error-color);
  }

  .list-title {
    margin: 0 0 0.5rem;
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .file-list {
    margin: 0 0 1.5rem;
    padding: 0;
    list-style: none;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
  }

  .file-list li {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 0.75rem;
    background-color: var(--background-color);
  }

  .file-list li + li {
    border-top: 1px solid var(--border-color);
  }

  .file-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-color);
    text-decoration: none;
  }

  .file-name:hover {
    color: var(--primary-on-surface);
    text-decoration: underline;
  }

  .file-age {
    font-size: 0.75rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .all-normal,
  .empty {
    margin: 0 0 1.5rem;
    font-size: 0.875rem;
    color: var(--text-secondary);
  }

  .error-message {
    padding: 0.75rem 1rem;
    border-radius: 8px;
    color: var(--error-color);
    border: 1px solid var(--error-color);
  }
</style>
