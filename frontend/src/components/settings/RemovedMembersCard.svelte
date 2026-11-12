<!--
  RemovedMembersCard.svelte — accounts an administrator removed, waiting out the grace period
  before their personal files are erased (issue #1104), with Restore and the grace-period setting.

  Rendered on the Privacy & Redaction "policy" tab (super administrator). The server decides every
  outcome (a restore after the grace period is a 409); this renders it.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import {
    getSelfErasureSettings,
    listRemovedMembers,
    putSelfErasureSettings,
    restoreRemovedMember,
    type RemovedMember,
    type SelfErasureSettings,
  } from '$lib/api/selfErasure';
  import { getErrorStatus } from '$lib/utils/apiError';
  import { formatDateTime } from '$lib/utils/formatting';
  import { locale, t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  let settings: SelfErasureSettings | null = null;
  let items: RemovedMember[] = [];
  let loading = true;
  let loadFailed = false;
  let busy = false;
  let days = 14;

  async function load() {
    loading = true;
    loadFailed = false;
    try {
      [settings, items] = await Promise.all([getSelfErasureSettings(), listRemovedMembers()]);
      days = settings.removed_member_grace_days;
    } catch (error) {
      console.error('Failed to load the removed members:', error);
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  async function saveGrace() {
    if (busy) return;
    busy = true;
    try {
      settings = await putSelfErasureSettings({ removed_member_grace_days: days });
      days = settings.removed_member_grace_days;
      toastStore.success($t('settings.removedMembers.saved'));
    } catch (error) {
      console.error('Failed to save the removed-member grace period:', error);
      toastStore.error($t('settings.removedMembers.saveError'));
    } finally {
      busy = false;
    }
  }

  async function restore(item: RemovedMember) {
    if (busy) return;
    busy = true;
    try {
      await restoreRemovedMember(item.ledger_uuid);
      items = items.filter((i) => i.ledger_uuid !== item.ledger_uuid);
      toastStore.success($t('settings.removedMembers.restored'));
    } catch (error) {
      console.error('Failed to restore the removed member:', error);
      if (getErrorStatus(error) === 409) {
        toastStore.error($t('settings.removedMembers.tooLate'));
        items = await listRemovedMembers().catch(() => items);
      } else {
        toastStore.error($t('settings.removedMembers.restoreFailed'));
      }
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<div class="removed-members" data-testid="removed-members-card">
  <h3 class="title">{$t('settings.removedMembers.title')}</h3>
  <p class="intro">{$t('settings.removedMembers.description')}</p>
  {#if loading}
    <SettingsSkeleton variant="rows" count={1} label={$t('common.loading')} />
  {:else if loadFailed || !settings}
    <p class="error" role="alert">{$t('settings.removedMembers.loadError')}</p>
  {:else}
    <div class="grace">
      <label for="removed-member-grace-days">{$t('settings.removedMembers.graceLabel')}</label>
      <input
        id="removed-member-grace-days"
        type="number"
        class="form-control"
        min="0"
        max={settings.max_grace_days}
        bind:value={days}
      />
      <button
        type="button"
        class="btn btn-secondary"
        disabled={busy || days === settings.removed_member_grace_days}
        on:click={saveGrace}
      >
        {$t('settings.removedMembers.saveGrace')}
      </button>
      <span class="hint">
        {$t('settings.removedMembers.graceHint', { max: settings.max_grace_days })}
      </span>
    </div>
    {#if items.length === 0}
      <p class="hint" data-testid="removed-members-empty">{$t('settings.removedMembers.empty')}</p>
    {:else}
      <ul class="settings-item-list">
        {#each items as item (item.ledger_uuid)}
          <li class="settings-item-row" data-testid="removed-member-row">
            <div class="settings-item-text">
              <strong>{item.display_name ?? $t('settings.removedMembers.unnamed')}</strong>
              <span class="hint">
                {$t('settings.removedMembers.erasedAt', {
                  when: formatDateTime(item.scheduled_for, '', $locale),
                })}
              </span>
            </div>
            <button
              type="button"
              class="btn btn-secondary"
              disabled={busy}
              on:click={() => restore(item)}
            >
              {$t('settings.removedMembers.restore')}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</div>

<style>
  .removed-members {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin-top: var(--space-6);
  }

  .title {
    margin: 0;
    font-size: var(--font-size-base);
    color: var(--text-color);
  }

  .intro {
    margin: 0;
    font-size: var(--font-size-sm);
    line-height: var(--line-height-normal);
    color: var(--text-secondary);
    max-width: var(--measure-prose);
  }

  .grace {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .grace input {
    width: 5rem;
    padding: var(--space-1-5) var(--space-2);
  }

  .hint {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .error {
    margin: 0;
    color: var(--error-text);
  }
</style>
