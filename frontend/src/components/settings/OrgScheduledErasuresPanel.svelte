<!--
  OrgScheduledErasuresPanel.svelte — an organization's scheduled erasures, with Cancel (issue #1105).

  Lists the erasures of the whole organization and of one member's organization data that are
  waiting to run, soonest first. Any organization administrator may cancel one: more people able
  to stop a destructive act is the safe direction. Requesting an erasure is deliberately NOT here
  (API only, owner only for the whole organization; backend/app/api/CLAUDE.md, Compliance). The
  server decides every outcome, this renders it.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { OrgAdminApi, type ScheduledErasure } from '$lib/api/orgAdmin';
  import { getErrorStatus } from '$lib/utils/apiError';
  import { formatDateTime } from '$lib/utils/formatting';
  import { locale, t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  let items: ScheduledErasure[] = [];
  let loading = true;
  let loadFailed = false;
  let cancelling: string | null = null;

  async function load() {
    loading = true;
    loadFailed = false;
    try {
      items = await OrgAdminApi.listScheduledErasures();
    } catch (error) {
      console.error('Failed to load the scheduled erasures:', error);
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  async function cancel(item: ScheduledErasure) {
    if (cancelling) return;
    cancelling = item.ledger_uuid;
    try {
      await OrgAdminApi.cancelScheduledErasure(item.ledger_uuid);
      items = items.filter((i) => i.ledger_uuid !== item.ledger_uuid);
      toastStore.success($t('orgErasures.cancelled'));
    } catch (error) {
      console.error('Failed to cancel the scheduled erasure:', error);
      // 409 = it has started (or finished) in the meantime: say so and show the true list.
      if (getErrorStatus(error) === 409) {
        toastStore.error($t('orgErasures.tooLate'));
        await load();
      } else {
        toastStore.error($t('orgErasures.cancelFailed'));
      }
    } finally {
      cancelling = null;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<div class="org-erasures" data-testid="org-erasures-panel">
  <p class="intro">{$t('orgErasures.description')}</p>
  {#if loading}
    <SettingsSkeleton variant="rows" count={2} label={$t('common.loading')} />
  {:else if loadFailed}
    <p class="error" role="alert">{$t('orgErasures.loadError')}</p>
    <button type="button" class="btn btn-secondary" on:click={load}>{$t('common.retry')}</button>
  {:else if items.length === 0}
    <EmptyState
      icon="trash"
      title={$t('orgErasures.emptyTitle')}
      description={$t('orgErasures.emptyDescription')}
    />
  {:else}
    <ul class="settings-item-list">
      {#each items as item (item.ledger_uuid)}
        <li class="settings-item-row" data-testid="org-erasure-row">
          <div class="settings-item-text">
            <strong>
              {item.subject_type === 'organization'
                ? $t('orgErasures.wholeOrganization')
                : $t('orgErasures.oneMember')}
            </strong>
            <span class="meta">{$t('orgErasures.runsAt')}: {formatDateTime(item.scheduled_for, '', $locale)}</span>
            <span class="meta">
              {$t('orgErasures.requestedBy')}: {item.requested_by ?? $t('orgErasures.formerMember')}
            </span>
          </div>
          <button
            type="button"
            class="btn btn-secondary"
            disabled={cancelling !== null}
            on:click={() => cancel(item)}
          >
            {$t('orgErasures.cancel')}
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .org-erasures {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    padding: var(--space-1) 0;
  }

  .intro {
    margin: 0;
    font-size: var(--font-size-sm);
    line-height: 1.5;
    color: var(--text-secondary);
    max-width: 72ch;
  }

  .error {
    margin: 0;
    color: var(--error-text);
  }

  .meta {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }
</style>
