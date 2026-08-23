<!--
  ShareDocumentModal.svelte — share a document with a user or group (v400, #362 lane
  C3-remainder).

  A self-contained sibling of $components/sharing/ShareCollectionModal.svelte, not a
  reuse of it: that modal and its CurrentSharesList are coupled to $lib/api/sharing.ts
  and $stores/sharing.ts, both collection-scoped. Documents have no collection concept
  (a document share grants access to exactly one document directly — see
  backend/app/models/document.py:DocumentShare), so this owns its own local
  `shares` state instead of a shared store. It DOES reuse the two genuinely generic
  pieces from that folder unmodified: ShareTargetSearch (user+group picker) and
  PermissionLevelSelect (the viewer/editor vocabulary) — importing them rather than
  forking a second copy.
-->
<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { toastStore } from '$stores/toast';
  import ShareTargetSearch from '$components/sharing/ShareTargetSearch.svelte';
  import PermissionLevelSelect from '$components/sharing/PermissionLevelSelect.svelte';
  import {
    listDocumentShares,
    createDocumentShare,
    updateDocumentShare,
    deleteDocumentShare,
  } from '$lib/api/documents';
  import type { DocumentShare, DocumentPermissionLevel } from '$lib/types/document';
  import type { ShareTargetSearchResult } from '$lib/types/groups';

  export let documentUuid: string;
  export let documentName: string;

  const dispatch = createEventDispatcher();

  let shares: DocumentShare[] = [];
  let loading = true;
  let sharing = false;
  let revokingUuid: string | null = null;

  let pendingTargets: Array<ShareTargetSearchResult & { permission: DocumentPermissionLevel }> = [];

  $: existingShareTargets = [
    ...shares.map((s) => ({ type: s.target_type, uuid: s.target_uuid })),
    ...pendingTargets.map((pt) => ({ type: pt.type, uuid: pt.uuid })),
  ];

  async function loadShares() {
    loading = true;
    try {
      shares = await listDocumentShares(documentUuid);
    } catch (err: unknown) {
      console.error('Error loading document shares:', err);
      toastStore.error($t('sharing.failedToLoadShares'));
    } finally {
      loading = false;
    }
  }

  function handleTargetSelect(event: CustomEvent<ShareTargetSearchResult>) {
    const target = event.detail;
    pendingTargets = [...pendingTargets, { ...target, permission: 'viewer' }];
  }

  function removePendingTarget(index: number) {
    pendingTargets = pendingTargets.filter((_, i) => i !== index);
  }

  function handlePendingPermissionChange(index: number, event: CustomEvent<DocumentPermissionLevel>) {
    pendingTargets = pendingTargets.map((pt, i) =>
      i === index ? { ...pt, permission: event.detail } : pt
    );
  }

  async function shareWithTargets() {
    if (pendingTargets.length === 0) return;

    sharing = true;
    try {
      let successCount = 0;
      const errors: string[] = [];

      // One request per target, same reasoning ShareCollectionModal gives: partial
      // success is normal and each failure should be attributable to its target.
      for (const target of pendingTargets) {
        try {
          const newShare = await createDocumentShare(documentUuid, {
            target_type: target.type,
            target_uuid: target.uuid,
            permission: target.permission,
          });
          shares = [...shares, newShare];
          successCount++;
        } catch (err: unknown) {
          const detail = getErrorMessage(err, $t('documents.failedToShare'));
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

  async function changePermission(share: DocumentShare, permission: DocumentPermissionLevel) {
    try {
      const updated = await updateDocumentShare(documentUuid, share.uuid, { permission });
      shares = shares.map((s) => (s.uuid === share.uuid ? updated : s));
      toastStore.success($t('sharing.permissionUpdated'));
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('sharing.failedToUpdatePermission')));
    }
  }

  async function revokeShare(share: DocumentShare) {
    revokingUuid = share.uuid;
    try {
      await deleteDocumentShare(documentUuid, share.uuid);
      shares = shares.filter((s) => s.uuid !== share.uuid);
      toastStore.success($t('sharing.shareRevoked'));
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('sharing.failedToRevoke')));
    } finally {
      revokingUuid = null;
    }
  }

  function handleClose() {
    dispatch('close');
  }

  onMount(() => {
    loadShares();
  });
</script>

<BaseModal isOpen={true} title={$t('documents.shareDocument')} onClose={handleClose} maxWidth="540px" zIndex={1300}>
  <div class="share-header">
    <p class="document-name">
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
      </svg>
      {documentName}
    </p>
    <p class="share-intro">{$t('documents.shareIntro')}</p>
  </div>

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

  <div class="search-section">
    <ShareTargetSearch {existingShareTargets} on:select={handleTargetSelect} />
  </div>

  {#if pendingTargets.length > 0}
    <div class="pending-section" transition:slide>
      <h4>{$t('sharing.pendingShares')}</h4>
      <div class="pending-list">
        {#each pendingTargets as target, index (target.type + '-' + target.uuid)}
          <div class="pending-item">
            <div class="pending-info">
              <span class="pending-name">{target.name}</span>
            </div>
            <div class="pending-actions">
              <PermissionLevelSelect
                value={target.permission}
                on:change={(e) => handlePendingPermissionChange(index, e)}
              />
              <button
                class="remove-btn"
                type="button"
                on:click={() => removePendingTarget(index)}
                title={$t('sharing.remove')}
                aria-label={$t('sharing.remove')}
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
        <button class="share-btn" type="button" on:click={shareWithTargets} disabled={sharing}>
          {#if sharing}
            <Spinner size="small" />
            {$t('sharing.sharing')}
          {:else}
            {$t('sharing.shareButton')}
          {/if}
        </button>
      </div>
    </div>
  {/if}

  <div class="divider"></div>

  {#if loading}
    <div class="loading-shares">
      <Spinner size="small" />
      {$t('sharing.loadingShares')}
    </div>
  {:else if shares.length === 0}
    <div class="empty-shares">
      <p class="empty-title">{$t('documents.notSharedYet')}</p>
      <p class="empty-desc">{$t('documents.notSharedYetDesc')}</p>
    </div>
  {:else}
    <ul class="shares-list">
      {#each shares as share (share.uuid)}
        <li class="share-row">
          <div class="share-target">
            <span class="target-name">{share.target_name}</span>
            {#if share.target_email}<span class="target-email">{share.target_email}</span>{/if}
            {#if share.member_count !== null}
              <span class="target-email">{$t('sharing.memberCount', { count: share.member_count })}</span>
            {/if}
          </div>
          <div class="share-controls">
            <PermissionLevelSelect
              value={share.permission}
              on:change={(e) => changePermission(share, e.detail)}
            />
            <button
              class="remove-btn"
              type="button"
              on:click={() => revokeShare(share)}
              disabled={revokingUuid === share.uuid}
              title={$t('sharing.revokeAccess')}
              aria-label={$t('sharing.revokeAccess')}
            >
              {#if revokingUuid === share.uuid}
                <Spinner size="small" />
              {:else}
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18"/>
                  <line x1="6" y1="6" x2="18" y2="18"/>
                </svg>
              {/if}
            </button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</BaseModal>

<style>
  .share-header {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-bottom: 1rem;
  }

  .document-name {
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
    overflow-wrap: anywhere;
  }

  .document-name svg {
    flex-shrink: 0;
    color: var(--primary-color, #3b82f6);
  }

  .share-intro {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .permission-guide {
    margin-bottom: 1rem;
    padding: 0.625rem 0.75rem;
    background: rgba(59, 130, 246, 0.05);
    border: 1px solid rgba(59, 130, 246, 0.15);
    border-radius: 6px;
  }

  :global(.dark) .permission-guide {
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

  .pending-section h4 {
    margin: 0 0 0.5rem 0;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .pending-list,
  .shares-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .pending-item,
  .share-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 0.4rem 0.6rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
  }

  .pending-info,
  .share-target {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    min-width: 0;
    flex: 1;
  }

  .pending-name,
  .target-name {
    font-size: 0.85rem;
    font-weight: 500;
    color: var(--text-color);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .target-email {
    font-size: 0.72rem;
    color: var(--text-secondary);
  }

  .pending-actions,
  .share-controls {
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

  .remove-btn:hover:not(:disabled) {
    color: var(--error-color, #ef4444);
    background: rgba(239, 68, 68, 0.08);
  }

  .remove-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
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
  }

  .share-btn:hover:not(:disabled) {
    background: #2563eb;
  }

  .share-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .divider {
    height: 1px;
    background: var(--border-color);
    margin: 0.25rem 0 0.75rem;
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

  .empty-shares {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 1.25rem 1rem;
    text-align: center;
    color: var(--text-secondary);
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
  }
</style>
