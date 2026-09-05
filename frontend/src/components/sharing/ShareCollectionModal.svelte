<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import BaseModal from '../ui/BaseModal.svelte';
  import { toastStore } from '$stores/toast';
  import { SharingApi } from '$lib/api/sharing';
  import { sharingStore } from '$stores/sharing';
  import ShareTargetSearch from './ShareTargetSearch.svelte';
  import CurrentSharesList from './CurrentSharesList.svelte';
  import PermissionLevelSelect from './PermissionLevelSelect.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import type { Share, ShareTargetSearchResult, PermissionLevel } from '$lib/types/groups';

  export let collectionUuid: string;
  export let collectionName: string;
  /**
   * Whether the current user may manage shares on this collection (add/remove
   * targets, change permissions). Must be computed by the caller from real
   * ownership data (e.g. `collection.my_permission === 'owner'`) — never
   * hardcode this true. Backend still enforces this server-side
   * (`_require_collection_owner`), but a non-owner should not be shown
   * controls that will 403 when used.
   */
  export let canManage: boolean = false;

  const dispatch = createEventDispatcher();

  let shares: Share[] = [];
  let loading = true;
  let sharing = false;

  // Pending targets to share with
  let pendingTargets: Array<ShareTargetSearchResult & { permission: PermissionLevel }> = [];

  // Derived existing targets for search filtering
  $: existingShareTargets = [
    ...shares.map(s => ({ type: s.target_type, uuid: s.target_uuid })),
    ...pendingTargets.map(pt => ({ type: pt.type, uuid: pt.uuid })),
  ];

  async function loadShares() {
    loading = true;
    // Clear stale shares immediately to prevent previous collection's data from flashing
    shares = [];
    sharingStore.setCurrentShares([]);
    try {
      const data = await SharingApi.fetchCollectionShares(collectionUuid);
      shares = data;
      sharingStore.setCurrentShares(data);
    } catch (err: unknown) {
      console.error('Error loading shares:', err);
      toastStore.error($t('sharing.failedToLoadShares'));
    } finally {
      loading = false;
    }
  }

  function handleTargetSelect(event: CustomEvent<ShareTargetSearchResult>) {
    const target = event.detail;
    // Add to pending with default viewer permission
    pendingTargets = [...pendingTargets, { ...target, permission: 'viewer' as PermissionLevel }];
  }

  function removePendingTarget(index: number) {
    pendingTargets = pendingTargets.filter((_, i) => i !== index);
  }

  function handlePendingPermissionChange(index: number, event: CustomEvent<PermissionLevel>) {
    pendingTargets = pendingTargets.map((pt, i) =>
      i === index ? { ...pt, permission: event.detail } : pt
    );
  }

  async function shareWithTargets() {
    if (pendingTargets.length === 0) return;

    sharing = true;
    try {
      let successCount = 0;
      let errors: string[] = [];

      for (const target of pendingTargets) {
        try {
          const newShare = await SharingApi.shareCollection(collectionUuid, {
            target_type: target.type,
            target_uuid: target.uuid,
            permission: target.permission,
          });
          sharingStore.addShare(newShare);
          shares = [...shares, newShare];
          successCount++;
        } catch (err: unknown) {
          console.error('Error sharing with target:', target, err);
          const detail = getErrorMessage(err, $t('sharing.failedToShare'));
          errors.push(`${target.name}: ${detail}`);
        }
      }

      if (successCount > 0) {
        toastStore.success($t('sharing.sharedSuccess', { count: successCount }));
        dispatch('shared');
      }
      if (errors.length > 0) {
        toastStore.error(errors.join('\n'));
      }
    } finally {
      pendingTargets = [];
      sharing = false;
    }
  }

  function handleClose() {
    dispatch('close');
  }

  // Reactively update shares from store
  $: storeShares = $sharingStore.currentCollectionShares;
  $: if (storeShares.length > 0 || !loading) {
    shares = storeShares;
  }

  onMount(() => {
    loadShares();
  });
</script>

<BaseModal isOpen={true} title={$t('sharing.shareCollection')} onClose={handleClose} maxWidth="540px" zIndex={1300}>
        <div class="share-header">
          <p class="collection-name">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
            </svg>
            {collectionName}
          </p>
          <p class="share-intro">{$t('sharing.shareIntro')}</p>
        </div>

        <!-- Permission levels reference card -->
        <div class="permission-guide">
          <div class="permission-guide-title">{$t('sharing.permissionLevels')}</div>
          <div class="permission-guide-row">
            <span class="permission-badge viewer">{$t('sharing.permissionViewer')}</span>
            <span class="permission-desc">{$t('sharing.permissionViewerDesc')}</span>
          </div>
          <div class="permission-guide-row">
            <span class="permission-badge editor">{$t('sharing.permissionEditor')}</span>
            <span class="permission-desc">{$t('sharing.permissionEditorDesc')}</span>
          </div>
        </div>

        <!-- Search for users/groups to share with -->
        <div class="search-section">
          <ShareTargetSearch
            {existingShareTargets}
            on:select={handleTargetSelect}
          />
        </div>

        <!-- Pending targets to share -->
        {#if pendingTargets.length > 0}
          <div class="pending-section" transition:slide>
            <h4>{$t('sharing.pendingShares')}</h4>
            <div class="pending-list">
              {#each pendingTargets as target, index (target.type + '-' + target.uuid)}
                <div class="pending-item">
                  <div class="pending-info">
                    {#if target.type === 'user'}
                      <svg class="type-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                        <circle cx="12" cy="7" r="4"/>
                      </svg>
                    {:else}
                      <svg class="type-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
                        <circle cx="9" cy="7" r="4"/>
                        <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
                        <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
                      </svg>
                    {/if}
                    <span class="pending-name">{target.name}</span>
                  </div>
                  <div class="pending-actions">
                    <PermissionLevelSelect
                      value={target.permission}
                      on:change={(e) => handlePendingPermissionChange(index, e)}
                    />
                    <button
                      class="remove-btn"
                      on:click={() => removePendingTarget(index)}
                      title={$t('sharing.remove')}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <line x1="18" y1="6" x2="6" y2="18"/>
                        <line x1="6" y1="6" x2="18" y2="18"/>
                      </svg>
                    </button>
                  </div>
                </div>
              {/each}
            </div>

            <div class="share-action">
              <button
                class="share-btn"
                on:click={shareWithTargets}
                disabled={sharing || pendingTargets.length === 0}
              >
                {#if sharing}
                  <Spinner size="small" />
                  {$t('sharing.sharing')}
                {:else}
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/>
                    <polyline points="16 6 12 2 8 6"/>
                    <line x1="12" y1="2" x2="12" y2="15"/>
                  </svg>
                  {$t('sharing.shareButton')}
                {/if}
              </button>
            </div>
          </div>
        {/if}

        <!-- Divider -->
        <div class="divider"></div>

        <!-- Existing shares -->
        {#if loading}
          <div class="loading-shares">
            <Spinner size="small" />
            {$t('sharing.loadingShares')}
          </div>
        {:else if shares.length === 0}
          <div class="empty-shares">
            <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path>
              <circle cx="9" cy="7" r="4"></circle>
              <line x1="19" y1="8" x2="19" y2="14"></line>
              <line x1="22" y1="11" x2="16" y2="11"></line>
            </svg>
            <p class="empty-title">{$t('sharing.notSharedYet')}</p>
            <p class="empty-desc">{$t('sharing.notSharedYetDesc')}</p>
          </div>
        {:else}
          <CurrentSharesList
            {shares}
            {canManage}
            {collectionUuid}
          />
        {/if}
</BaseModal>

<style>
  .share-header {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-bottom: 1rem;
  }

  .collection-name {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0;
    padding: 0.5rem 0.75rem;
    font-size: 0.9rem;
    color: var(--text-primary);
    font-weight: 600;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
  }

  .collection-name svg {
    flex-shrink: 0;
    color: var(--primary-color, #3b82f6);
  }

  .share-intro {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  /* Permission guide */
  .permission-guide {
    margin-bottom: 1rem;
    padding: 0.625rem 0.75rem;
    background: rgba(59, 130, 246, 0.05);
    border: 1px solid rgba(59, 130, 246, 0.15);
    border-radius: 6px;
  }

  :global([data-theme='dark']) .permission-guide {
    background: rgba(59, 130, 246, 0.08);
    border-color: rgba(59, 130, 246, 0.2);
  }

  .permission-guide-title {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--text-secondary);
    margin-bottom: 0.375rem;
  }

  .permission-guide-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.25rem;
    font-size: 0.75rem;
    line-height: 1.4;
  }

  .permission-badge {
    flex-shrink: 0;
    display: inline-block;
    min-width: 52px;
    text-align: center;
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    font-weight: 600;
    font-size: 0.6875rem;
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }

  .permission-badge.viewer {
    background: rgba(100, 116, 139, 0.12);
    color: var(--text-secondary);
    border: 1px solid rgba(100, 116, 139, 0.25);
  }

  .permission-badge.editor {
    background: rgba(59, 130, 246, 0.12);
    color: var(--primary-color, #3b82f6);
    border: 1px solid rgba(59, 130, 246, 0.3);
  }

  .permission-desc {
    color: var(--text-secondary);
  }

  .search-section {
    margin-bottom: 0.25rem;
  }

  /* Empty state for no existing shares */
  .empty-shares {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 1.25rem 1rem;
    text-align: center;
    color: var(--text-secondary);
  }

  .empty-shares svg {
    color: var(--text-tertiary, #94a3b8);
    margin-bottom: 0.5rem;
  }

  .empty-title {
    margin: 0 0 0.25rem 0;
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .empty-desc {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-secondary);
    line-height: 1.4;
    max-width: 320px;
  }

  .pending-section h4 {
    margin: 0 0 0.5rem 0;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .pending-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .pending-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 0.4rem 0.6rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
  }

  .pending-info {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    flex: 1;
  }

  .type-icon {
    flex-shrink: 0;
    color: var(--text-secondary);
  }

  .pending-name {
    font-size: 0.85rem;
    font-weight: 500;
    color: var(--text-color);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .pending-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }

  .remove-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 26px;
    height: 26px;
    padding: 0;
    border: none;
    border-radius: 4px;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .remove-btn:hover {
    color: var(--error-color, #ef4444);
    background: rgba(239, 68, 68, 0.08);
  }

  .share-action {
    display: flex;
    justify-content: flex-end;
    margin-top: 0.5rem;
  }

  .share-btn {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 1.2rem;
    background: #3b82f6;
    color: white;
    border: none;
    border-radius: 8px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .share-btn:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .share-btn:active:not(:disabled) {
    transform: scale(1);
  }

  .share-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
  }

  .divider {
    height: 1px;
    background: var(--border-color);
    margin: 0.25rem 0;
  }

  .loading-shares {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 1rem 0;
    font-size: 0.85rem;
    color: var(--text-secondary);
    justify-content: center;
  }

  :global([data-theme='dark']) .pending-item {
    background: rgba(255, 255, 255, 0.03);
  }

  @media (max-width: 768px) {
    .pending-item {
      flex-direction: column;
      align-items: stretch;
      gap: 0.5rem;
    }

    .pending-actions {
      justify-content: flex-end;
    }

    .share-btn {
      width: 100%;
      min-height: 44px;
      justify-content: center;
    }

    .remove-btn {
      min-width: 44px;
      min-height: 44px;
    }
  }
</style>
