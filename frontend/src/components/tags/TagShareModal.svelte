<script lang="ts">
  /**
   * Share one tag with specific users and groups.
   *
   * The middle tier between "mine alone" and "Share with everyone", which
   * publishes to the whole deployment. Sharing a tag gives the recipient the
   * *word* — they see it, filter by it and apply it, so they use yours instead
   * of coining a duplicate. Renaming, merging and deleting stay with the owner,
   * which is why there is no permission picker here (unlike collection
   * sharing, where viewer/editor is a real distinction).
   *
   * Reuses `ShareTargetSearch` so finding a user or group works exactly as it
   * does when sharing a collection.
   */
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { listTagShares, shareTag, revokeTagShare } from '$lib/api/tags';
  import type { TagShareTarget } from '$lib/types/tag';
  import type { ShareTargetSearchResult } from '$lib/types/groups';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import ShareTargetSearch from '$components/sharing/ShareTargetSearch.svelte';
  import Spinner from '$components/ui/Spinner.svelte';

  export let tagUuid: string;
  export let tagName: string;

  const dispatch = createEventDispatcher<{ close: void; changed: void }>();

  let shares: TagShareTarget[] = [];
  let loading = true;
  let busy = false;

  // Feeds the search's own de-duplication, so an existing recipient is not
  // offered again.
  $: existingTargets = shares.map((share) => ({ type: share.target_type, uuid: share.uuid }));

  async function load() {
    loading = true;
    try {
      shares = await listTagShares(tagUuid);
    } catch (error: unknown) {
      toastStore.error(getErrorMessage(error, $t('tags.manager.share.loadFailed')));
      shares = [];
    } finally {
      loading = false;
    }
  }

  onMount(load);

  async function addTarget(event: CustomEvent<ShareTargetSearchResult>) {
    const target = event.detail;
    busy = true;
    try {
      await shareTag(tagUuid, {
        target_user_uuid: target.type === 'user' ? target.uuid : undefined,
        target_group_uuid: target.type === 'group' ? target.uuid : undefined,
      });
      toastStore.success($t('tags.manager.share.added', { name: target.name }));
      await load();
      dispatch('changed');
    } catch (error: unknown) {
      toastStore.error(getErrorMessage(error, $t('tags.manager.share.addFailed')));
    } finally {
      busy = false;
    }
  }

  async function revoke(share: TagShareTarget) {
    busy = true;
    try {
      await revokeTagShare(tagUuid, share.uuid);
      toastStore.success($t('tags.manager.share.revoked', { name: share.display_name }));
      await load();
      dispatch('changed');
    } catch (error: unknown) {
      toastStore.error(getErrorMessage(error, $t('tags.manager.share.revokeFailed')));
    } finally {
      busy = false;
    }
  }
</script>

<BaseModal
  isOpen
  maxWidth="520px"
  title={$t('tags.manager.share.title', { name: tagName })}
  onClose={() => dispatch('close')}
>
  <div class="tag-share">
    <p class="share-note">{$t('tags.manager.share.description')}</p>

    <ShareTargetSearch existingShareTargets={existingTargets} on:select={addTarget} />

    {#if loading}
      <div class="share-loading"><Spinner size="small" /></div>
    {:else if shares.length === 0}
      <p class="share-empty">{$t('tags.manager.share.none')}</p>
    {:else}
      <ul class="share-list">
        {#each shares as share (share.uuid)}
          <li class="share-row">
            <span class="share-target">
              <span class="share-kind">
                {share.target_type === 'group'
                  ? $t('tags.manager.share.group')
                  : $t('tags.manager.share.user')}
              </span>
              <span class="share-name">{share.display_name}</span>
            </span>
            <button
              type="button"
              class="btn btn-ghost"
              on:click={() => revoke(share)}
              disabled={busy}
              title={$t('tags.manager.share.revokeTooltip')}
            >
              {$t('tags.manager.share.revoke')}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</BaseModal>

<style>
  .tag-share {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .share-note,
  .share-empty {
    margin: 0;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .share-list {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .share-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 6px 8px;
    border: 1px solid var(--border-color);
    border-radius: 6px;
  }

  .share-target {
    display: flex;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
  }

  .share-kind {
    flex-shrink: 0;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-secondary);
  }

  .share-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .share-loading {
    padding: 4px 0;
  }
</style>
