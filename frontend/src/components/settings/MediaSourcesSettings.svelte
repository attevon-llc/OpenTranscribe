<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import Spinner from '$components/ui/Spinner.svelte';
  import {
    getMediaSources,
    addMediaSource,
    updateMediaSource,
    deleteMediaSource,
    toggleMediaSourceShare,
    type MediaSource,
    type MediaSourceUpdate
  } from '$lib/api/mediaSourcesApi';
  import { isPlainHttpSource, isPrivateHostRefusal } from '$lib/utils/mediaHost';
  import { user } from '$stores/auth';
  import MediaPrivateHostsCard from './MediaPrivateHostsCard.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';

  let loading = true;
  $: isSuperAdmin = $user?.role === 'super_admin';
  let sources: MediaSource[] = [];
  let sharedSources: MediaSource[] = [];
  let saving = false;

  // Add form state
  let showAddForm = false;
  let newHostname = '';
  let newLabel = '';
  let newUsername = '';
  let newPassword = '';
  let newVerifySsl = true;
  let newProviderType = 'mediacms';

  // Edit state
  let editingUuid: string | null = null;
  let editHostname = '';
  let editLabel = '';
  let editUsername = '';
  let editPassword = '';
  let editVerifySsl = true;

  // Delete confirmation
  let deletingUuid: string | null = null;

  onMount(async () => {
    await loadSources();
  });

  async function loadSources() {
    loading = true;
    try {
      const resp = await getMediaSources();
      sources = resp.sources || [];
      sharedSources = resp.shared_sources || [];
    } catch (err) {
      console.error('Failed to load media sources:', err);
      toastStore.error($t('settings.mediaSources.loadFailed'));
    } finally {
      loading = false;
    }
  }

  function resetAddForm() {
    newHostname = '';
    newLabel = '';
    newUsername = '';
    newPassword = '';
    newVerifySsl = true;
    newProviderType = 'mediacms';
    showAddForm = false;
  }

  async function handleAdd() {
    if (!newHostname.trim()) return;
    saving = true;
    try {
      const created = await addMediaSource({
        hostname: newHostname.trim().toLowerCase(),
        provider_type: newProviderType,
        username: newUsername,
        password: newPassword,
        // An http:// source has no TLS to verify; send the default so the saved value
        // (and the "not verified" badge) never reflects a hidden, stale checkbox.
        verify_ssl: isPlainHttpSource(newHostname) ? true : newVerifySsl,
        label: newLabel.trim(),
      });
      sources = [...sources, created];
      resetAddForm();
      toastStore.success($t('settings.mediaSources.addSuccess'));
    } catch (err: unknown) {
      toastStore.error(
        isPrivateHostRefusal(err)
          ? $t('settings.mediaSources.privateHostRefused')
          : getErrorMessage(err, $t('settings.mediaSources.addFailed'))
      );
    } finally {
      saving = false;
    }
  }

  function startEdit(source: MediaSource) {
    showAddForm = false;
    deletingUuid = null;
    editingUuid = source.uuid;
    editHostname = source.hostname;
    editLabel = source.label;
    editUsername = source.username || '';
    editPassword = '';
    editVerifySsl = source.verify_ssl;
  }

  function cancelEdit() {
    editingUuid = null;
  }

  async function handleUpdate() {
    if (!editingUuid || !editHostname.trim()) return;
    saving = true;
    try {
      const updateData: MediaSourceUpdate = {
        hostname: editHostname.trim().toLowerCase(),
        label: editLabel.trim(),
        username: editUsername,
        verify_ssl: isPlainHttpSource(editHostname) ? true : editVerifySsl,
      };
      // Only send password if user typed something new
      if (editPassword) {
        updateData.password = editPassword;
      }
      const updated = await updateMediaSource(editingUuid, updateData);
      sources = sources.map(s => s.uuid === editingUuid ? updated : s);
      editingUuid = null;
      toastStore.success($t('settings.mediaSources.updateSuccess'));
    } catch (err: unknown) {
      toastStore.error(
        isPrivateHostRefusal(err)
          ? $t('settings.mediaSources.privateHostRefused')
          : getErrorMessage(err, $t('settings.mediaSources.updateFailed'))
      );
    } finally {
      saving = false;
    }
  }

  async function handleDelete(uuid: string) {
    saving = true;
    try {
      await deleteMediaSource(uuid);
      sources = sources.filter(s => s.uuid !== uuid);
      deletingUuid = null;
      toastStore.success($t('settings.mediaSources.deleteSuccess'));
    } catch (err) {
      toastStore.error($t('settings.mediaSources.deleteFailed'));
    } finally {
      saving = false;
    }
  }

  async function handleShareToggle(source: MediaSource) {
    if (saving) return;
    saving = true;
    const newShared = !source.is_shared;
    // Optimistic update
    sources = sources.map(s => s.uuid === source.uuid ? { ...s, is_shared: newShared } : s);
    try {
      const updated = await toggleMediaSourceShare(source.uuid, newShared);
      sources = sources.map(s => s.uuid === source.uuid ? updated : s);
      toastStore.success(newShared
        ? $t('settings.mediaSources.shareEnabled')
        : $t('settings.mediaSources.shareDisabled'));
    } catch {
      // Rollback
      sources = sources.map(s => s.uuid === source.uuid ? { ...s, is_shared: !newShared } : s);
      toastStore.error($t('settings.mediaSources.shareFailed'));
    } finally {
      saving = false;
    }
  }
</script>

{#snippet metaBadges(source: MediaSource)}
  <div class="source-meta">
    {#if source.has_credentials}
      <span class="meta-badge creds">{$t('settings.mediaSources.credentialsStored')}</span>
    {:else}
      <span class="meta-badge no-creds">{$t('settings.mediaSources.noCreds')}</span>
    {/if}
    {#if !source.verify_ssl}
      <span class="meta-badge ssl-off">{$t('settings.mediaSources.sslOff')}</span>
    {/if}
    {#if isPlainHttpSource(source.hostname)}
      <span class="meta-badge http-plain">{$t('settings.mediaSources.plainHttpBadge')}</span>
    {/if}
  </div>
{/snippet}

{#if loading}
  <div class="loading-state">
    <Spinner size="small" />
    {$t('settings.mediaSources.loading')}
  </div>
{:else}
  <div class="media-sources">
    <!-- Own Sources -->
    {#if sources.length > 0}
      {#each sources as source (source.uuid)}
        <div class="source-card" class:editing={editingUuid === source.uuid}>
          {#if editingUuid === source.uuid}
            <!-- Edit Mode -->
            <form class="source-form" on:submit|preventDefault={handleUpdate}>
              <div class="form-row">
                <div class="form-field">
                  <label for="edit-hostname">{$t('settings.mediaSources.hostname')}</label>
                  <input dir="ltr" id="edit-hostname" type="text" bind:value={editHostname} class="form-input" placeholder="media.example.com" aria-describedby="edit-hostname-hint" />
                  <p id="edit-hostname-hint" class="field-hint">{$t('settings.mediaSources.hostnameHint')}</p>
                  {#if isPlainHttpSource(editHostname)}
                    <p class="http-warning" role="alert">{$t('settings.mediaSources.plainHttpWarning')}</p>
                  {/if}
                </div>
                <div class="form-field">
                  <label for="edit-label">{$t('settings.mediaSources.label')}</label>
                  <input id="edit-label" type="text" bind:value={editLabel} class="form-input" placeholder={$t('settings.mediaSources.labelPlaceholder')} />
                </div>
              </div>
              <div class="form-row">
                <div class="form-field">
                  <label for="edit-username">{$t('settings.mediaSources.username')}</label>
                  <input id="edit-username" type="text" bind:value={editUsername} class="form-input" autocomplete="off" />
                </div>
                <div class="form-field">
                  <label for="edit-password">{$t('settings.mediaSources.password')}</label>
                  <input dir="ltr" id="edit-password" type="password" bind:value={editPassword} class="form-input" autocomplete="new-password"
                    placeholder={source.has_credentials ? '••••••••' : ''} />
                </div>
              </div>
              <div class="form-row">
                {#if !isPlainHttpSource(editHostname)}
                <label class="toggle-row-inline">
                  <input type="checkbox" bind:checked={editVerifySsl} />
                  <span>{$t('settings.mediaSources.verifySsl')}</span>
                </label>
                {/if}
              </div>
              <div class="form-actions-inline">
                <button type="button" class="btn btn-sm btn-secondary" on:click={cancelEdit} disabled={saving}>{$t('common.cancel')}</button>
                <button type="submit" class="btn btn-sm btn-primary" disabled={saving || !editHostname.trim()}>
                  {saving ? $t('common.saving') : $t('common.save')}
                </button>
              </div>
            </form>
          {:else}
            <!-- View Mode -->
            <div class="source-header">
              <div class="source-info">
                <div class="source-hostname">
                  <span class="hostname-badge">{source.provider_type}</span>
                  {source.hostname}
                  {#if source.is_shared}
                    <span class="shared-badge">{$t('settings.mediaSources.shared')}</span>
                  {/if}
                </div>
                {#if source.label}
                  <div class="source-label">{source.label}</div>
                {/if}
              </div>
              {@render metaBadges(source)}
            </div>
            <div class="source-actions">
              <button class="btn btn-sm btn-secondary" on:click={() => startEdit(source)} disabled={saving}>
                {$t('common.edit')}
              </button>
              {#if deletingUuid === source.uuid}
                <span class="delete-confirm">
                  {$t('settings.mediaSources.confirmDelete')}
                  <button class="btn btn-sm btn-danger" on:click={() => handleDelete(source.uuid)} disabled={saving}>
                    {$t('common.yes')}
                  </button>
                  <button class="btn btn-sm btn-secondary" on:click={() => deletingUuid = null}>
                    {$t('common.no')}
                  </button>
                </span>
              {:else}
                <button class="btn btn-sm btn-danger-outline" on:click={() => deletingUuid = source.uuid} disabled={saving}>
                  {$t('common.delete')}
                </button>
              {/if}
              <div class="share-toggle">
                <label class="toggle-row-inline share-label">
                  <input type="checkbox" checked={source.is_shared}
                    on:change={() => handleShareToggle(source)} disabled={saving} />
                  <span>{$t('settings.mediaSources.shareGlobally')}</span>
                </label>
              </div>
            </div>
          {/if}
        </div>
      {/each}
    {/if}

    <!-- Shared by Others -->
    {#if sharedSources.length > 0}
      <div class="shared-section-header">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10" />
          <line x1="2" y1="12" x2="22" y2="12" />
          <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
        </svg>
        <span>{$t('settings.mediaSources.sharedByOthers')}</span>
      </div>

      {#each sharedSources as source (source.uuid)}
        <div class="source-card shared">
          <div class="source-header">
            <div class="source-info">
              <div class="source-hostname">
                <span class="hostname-badge">{source.provider_type}</span>
                {source.hostname}
                {#if source.owner_role === 'admin' || source.owner_role === 'super_admin'}
                  <span class="admin-badge">{$t('settings.sharing.adminBadge')}</span>
                {/if}
              </div>
              {#if source.label}
                <div class="source-label">{source.label}</div>
              {/if}
              {#if source.owner_name}
                <div class="shared-by">{$t('settings.mediaSources.sharedBy')} {source.owner_name}</div>
              {/if}
            </div>
            {@render metaBadges(source)}
          </div>
        </div>
      {/each}
    {/if}

    <!-- Empty state -->
    {#if sources.length === 0 && sharedSources.length === 0 && !showAddForm}
      <EmptyState bordered titleSize="lg" padding="var(--space-10) var(--space-8)" title={$t('settings.mediaSources.noSources')} description={$t('settings.mediaSources.noSourcesHint')}>
        <svg slot="icon" width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101" />
          <path d="M10.172 13.828a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.102 1.101" />
        </svg>
        <button class="btn btn-primary" on:click={() => { showAddForm = true; editingUuid = null; deletingUuid = null; }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          {$t('settings.mediaSources.addSource')}
        </button>
      </EmptyState>
    {/if}

    <!-- Add Form -->
    {#if showAddForm}
      <form class="source-card add-card" on:submit|preventDefault={handleAdd}>
        <div class="source-form">
          <div class="form-row">
            <div class="form-field">
              <label for="new-hostname">{$t('settings.mediaSources.hostname')}</label>
              <input dir="ltr" id="new-hostname" type="text" bind:value={newHostname} class="form-input" placeholder="media.example.com" aria-describedby="new-hostname-hint" />
              <p id="new-hostname-hint" class="field-hint">{$t('settings.mediaSources.hostnameHint')}</p>
              {#if isPlainHttpSource(newHostname)}
                <p class="http-warning" role="alert">{$t('settings.mediaSources.plainHttpWarning')}</p>
              {/if}
            </div>
            <div class="form-field">
              <label for="new-label">{$t('settings.mediaSources.label')}</label>
              <input id="new-label" type="text" bind:value={newLabel} class="form-input" placeholder={$t('settings.mediaSources.labelPlaceholder')} />
            </div>
          </div>
          <div class="form-row">
            <div class="form-field">
              <label for="new-username">{$t('settings.mediaSources.username')}</label>
              <input id="new-username" type="text" bind:value={newUsername} class="form-input" autocomplete="off" />
            </div>
            <div class="form-field">
              <label for="new-password">{$t('settings.mediaSources.password')}</label>
              <input dir="ltr" id="new-password" type="password" bind:value={newPassword} class="form-input" autocomplete="new-password" />
            </div>
          </div>
          <div class="form-row">
            <div class="form-field">
              <label for="new-provider">{$t('settings.mediaSources.providerType')}</label>
              <select id="new-provider" bind:value={newProviderType} class="form-select">
                <option value="mediacms">MediaCMS</option>
              </select>
            </div>
            {#if !isPlainHttpSource(newHostname)}
            <label class="toggle-row-inline">
              <input type="checkbox" bind:checked={newVerifySsl} />
              <span>{$t('settings.mediaSources.verifySsl')}</span>
            </label>
            {/if}
          </div>
          <div class="form-actions-inline">
            <button type="button" class="btn btn-sm btn-secondary" on:click={resetAddForm} disabled={saving}>{$t('common.cancel')}</button>
            <button type="submit" class="btn btn-sm btn-primary" disabled={saving || !newHostname.trim()}>
              {saving ? $t('settings.mediaSources.adding') : $t('settings.mediaSources.addSource')}
            </button>
          </div>
        </div>
      </form>
    {/if}

    <!-- Add Button — shown below existing sources list -->
    {#if (sources.length > 0 || sharedSources.length > 0) && !showAddForm}
      <button class="btn btn-primary" on:click={() => { showAddForm = true; editingUuid = null; deletingUuid = null; }}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
        {$t('settings.mediaSources.addSource')}
      </button>
    {/if}

    <p class="section-hint">{$t('settings.mediaSources.hint')}</p>
  </div>
{/if}

{#if isSuperAdmin}
  <MediaPrivateHostsCard />
{/if}

<style>
  .loading-state {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-4) 0;
    color: var(--text-secondary);
  }

  .media-sources {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .source-card {
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    padding: var(--space-3) var(--space-4);
    background: var(--card-background);
    transition: border-color var(--duration-fast);
  }

  .source-card:hover:not(.editing):not(.add-card) {
    border-color: var(--primary-color);
  }

  .source-card.editing,
  .source-card.add-card {
    border-color: var(--primary-color);
    border-width: 2px;
    padding: calc(0.75rem - 1px) calc(1rem - 1px);
  }

  .source-card.shared {
    border-left: 3px solid var(--info-color);
    background: rgba(var(--accent-rgb), 0.04);
  }

  :global([data-theme='dark']) .source-card.shared {
    background: rgba(var(--info-color-rgb), 0.06);
  }

  .shared-section-header {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: 0.8rem;
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    margin-top: var(--space-2);
  }

  .source-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--space-3);
  }

  .source-info {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
    min-width: 0;
  }

  .source-hostname {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
  }

  .hostname-badge {
    font-size: 0.65rem;
    text-transform: uppercase;
    font-weight: var(--font-weight-semibold);
    background: var(--primary-color);
    color: var(--text-on-solid);
    padding: 0.1rem 0.35rem;
    border-radius: var(--radius-sm);
    letter-spacing: 0.02em;
    flex-shrink: 0;
  }

  .shared-badge {
    font-size: 0.6rem;
    text-transform: uppercase;
    font-weight: var(--font-weight-semibold);
    background: rgba(var(--success-color-rgb), 0.15);
    color: var(--success-text);
    padding: 0.1rem 0.35rem;
    border-radius: var(--radius-sm);
    letter-spacing: 0.02em;
  }

  :global([data-theme='dark']) .shared-badge {
    background: rgba(var(--success-color-rgb), 0.2);
  }

  .admin-badge {
    font-size: 0.6rem;
    text-transform: uppercase;
    font-weight: var(--font-weight-semibold);
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-on-surface);
    padding: 0.1rem 0.35rem;
    border-radius: var(--radius-sm);
    letter-spacing: 0.02em;
  }

  .source-label {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .shared-by {
    font-size: 0.7rem;
    color: var(--text-secondary);
    font-style: italic;
  }

  .source-meta {
    display: flex;
    gap: var(--space-1-5);
    flex-shrink: 0;
  }

  .meta-badge {
    font-size: 0.65rem;
    padding: 0.15rem 0.4rem;
    border-radius: var(--radius-sm);
    font-weight: var(--font-weight-medium);
    white-space: nowrap;
  }

  .meta-badge.creds {
    background: rgba(var(--success-color-rgb), 0.15);
    color: var(--success-text);
  }

  .meta-badge.no-creds {
    background: rgba(var(--warning-color-rgb), 0.15);
    color: var(--warning-text);
  }

  .meta-badge.ssl-off {
    background: rgba(var(--error-color-rgb), 0.15);
    color: var(--error-text);
  }

  .meta-badge.http-plain {
    background: var(--color-warning-bg);
    color: var(--color-warning-text);
  }

  .field-hint {
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
    margin: var(--space-1) 0 0;
    line-height: 1.4;
  }

  .http-warning {
    font-size: var(--font-size-xs);
    color: var(--color-warning-text);
    background: var(--color-warning-bg);
    border: 1px solid var(--color-warning-border);
    border-radius: var(--radius-md);
    padding: var(--space-1-5) var(--space-2-5);
    margin: var(--space-1-5) 0 0;
    line-height: 1.4;
  }

  .source-actions {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    margin-top: var(--space-2);
    padding-top: var(--space-2);
    border-top: 1px solid var(--border-color);
    flex-wrap: wrap;
  }

  .share-toggle {
    margin-left: auto;
  }

  .share-label {
    font-size: var(--font-size-xs);
  }

  .delete-confirm {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .source-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-2-5);
  }

  .form-row {
    display: flex;
    gap: var(--space-3);
    align-items: flex-end;
  }

  .form-field {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .form-field label {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .form-input,
  .form-select {
    padding: var(--space-1-5) var(--space-2);
    font-size: 0.8rem;
  }

  .toggle-row-inline {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: 0.8rem;
    color: var(--text-color);
    cursor: pointer;
  }

  .toggle-row-inline input[type="checkbox"] {
    width: 1rem;
    height: 1rem;
    accent-color: var(--primary-color);
  }

  .form-actions-inline {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-2);
    padding-top: var(--space-1);
  }

  /* Pattern B — grey ghost (inline actions like Edit, No) */

  /* Destructive text-only indicator */

  /* Destructive solid — red confirm */

  .section-hint {
    font-size: 0.7rem;
    color: var(--text-secondary);
    margin: var(--space-1) 0 0;
    line-height: 1.4;
  }

  @media (max-width: 768px) {
    .form-row {
      flex-direction: column;
    }

    .source-header {
      flex-direction: column;
    }

    .source-meta {
      flex-wrap: wrap;
    }

    .source-actions {
      flex-direction: column;
      align-items: flex-start;
    }

    .share-toggle {
      margin-left: 0;
    }

    .source-actions .btn,
    .source-actions button {
      min-height: 44px;
    }
  }
</style>
