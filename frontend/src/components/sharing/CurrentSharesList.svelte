<script lang="ts">
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import { SharingApi } from '$lib/api/sharing';
  import { sharingStore } from '$stores/sharing';
  import PermissionLevelSelect from './PermissionLevelSelect.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import Spinner from '$components/ui/Spinner.svelte';
  import type { Share, PermissionLevel } from '$lib/types/groups';

  export let shares: Share[] = [];
  export let canManage: boolean = false;
  export let collectionUuid: string;

  let updatingShareId: string | null = null;
  let revokingShareId: string | null = null;
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    variant: confirmVariant,
  } = confirmDialog;

  async function handlePermissionChange(share: Share, event: CustomEvent<PermissionLevel>) {
    const newPermission = event.detail;
    if (newPermission === share.permission) return;

    updatingShareId = share.uuid;
    try {
      await SharingApi.updateSharePermission(collectionUuid, share.uuid, {
        permission: newPermission,
      });
      sharingStore.updateSharePermission(share.uuid, newPermission);
      toastStore.success($t('sharing.permissionUpdated'));
    } catch (err: unknown) {
      console.error('Error updating share permission:', err);
      toastStore.error(getErrorMessage(err, $t('sharing.failedToUpdatePermission')));
    } finally {
      updatingShareId = null;
    }
  }

  function requestRevoke(share: Share) {
    confirmDialog.request(
      $t('sharing.revokeAccess'),
      $t('sharing.revokeConfirm', { name: share.target_name }),
      () => executeRevoke(share),
      { confirmText: $t('sharing.revokeAccess'), variant: 'danger' }
    );
  }

  async function executeRevoke(share: Share) {
    revokingShareId = share.uuid;
    try {
      await SharingApi.revokeShare(collectionUuid, share.uuid);
      sharingStore.removeShare(share.uuid);
      toastStore.success($t('sharing.shareRevoked'));
    } catch (err: unknown) {
      console.error('Error revoking share:', err);
      toastStore.error(getErrorMessage(err, $t('sharing.failedToRevoke')));
    } finally {
      revokingShareId = null;
    }
  }
</script>

{#if shares.length > 0}
  <div class="shares-list">
    <h4 class="list-title">{$t('sharing.currentShares')}</h4>
    {#each shares as share (share.uuid)}
      <div class="share-row" class:updating={updatingShareId === share.uuid}>
        <div class="share-target">
          {#if share.target_type === 'user'}
            <svg class="target-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
              <circle cx="12" cy="7" r="4"/>
            </svg>
          {:else}
            <svg class="target-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
              <circle cx="9" cy="7" r="4"/>
              <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
              <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
            </svg>
          {/if}
          <div class="target-info">
            <span class="target-name">{share.target_name}</span>
            {#if share.target_email}
              <span class="target-email">{share.target_email}</span>
            {/if}
            {#if share.member_count != null}
              <span class="target-email">
                {$t('sharing.memberCount', { count: share.member_count })}
              </span>
            {/if}
          </div>
        </div>

        <div class="share-actions">
          {#if canManage}
            <PermissionLevelSelect
              value={share.permission}
              disabled={updatingShareId === share.uuid}
              on:change={(e) => handlePermissionChange(share, e)}
            />
            <button
              class="btn btn-icon-only btn-sm btn-icon-danger revoke-btn"
              on:click={() => requestRevoke(share)}
              disabled={revokingShareId === share.uuid}
              title={$t('sharing.revokeAccess')}
              aria-label={$t('sharing.revokeAccess')}
            >
              {#if revokingShareId === share.uuid}
                <Spinner size="small" />
              {:else}
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18"/>
                  <line x1="6" y1="6" x2="18" y2="18"/>
                </svg>
              {/if}
            </button>
          {:else}
            <span class="permission-label">
              {$t(share.permission === 'viewer' ? 'sharing.permissionViewer' : share.permission === 'editor' ? 'sharing.permissionEditor' : 'sharing.permissionViewer')}
            </span>
          {/if}
        </div>
      </div>
    {/each}
  </div>
{/if}

<ConfirmationModal
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$confirmLabel}
  variant={$confirmVariant}
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>
  .shares-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .list-title {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin: 0 0 var(--space-2) 0;
  }

  .share-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-2-5);
    border-radius: var(--radius-md);
    transition: background-color var(--duration-fast) ease;
  }

  .share-row:hover {
    background-color: var(--hover-color);
  }

  .share-row.updating {
    opacity: 0.7;
  }

  .share-target {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
    flex: 1;
  }

  .target-icon {
    flex-shrink: 0;
    color: var(--text-secondary);
  }

  .target-info {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .target-name {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .target-email {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .share-actions {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    flex-shrink: 0;
  }

  .permission-label {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    padding: var(--space-1-5) var(--space-2);
  }

</style>
