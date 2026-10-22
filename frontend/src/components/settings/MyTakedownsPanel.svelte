<script lang="ts">
  /**
   * The signed-in user's own takedowns (issue #1254). A taken-down file 404s for its owner
   * everywhere, and the live in-app notice only reaches a session that happened to be open,
   * so this list is the owner's durable way to learn why a file vanished and how to dispute it.
   * It shows what the owner was told, never the operator's internal basis or the requester.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Spinner from '$components/ui/Spinner.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import { TakedownNoticesApi, type OwnerTakedown } from '$lib/api/takedownNotices';
  import TakedownStatusChip from '$components/takedown/TakedownStatusChip.svelte';
  import { TAKEDOWN_NOTICE_TYPE_KEYS, labelFor } from '$lib/i18n/keyMaps';

  const PAGE_SIZE = 50;

  let items: OwnerTakedown[] = [];
  let loading = true;
  let loadFailed = false;

  onMount(async () => {
    try {
      items = (await TakedownNoticesApi.listMine({ limit: PAGE_SIZE })).takedowns;
    } catch (err) {
      loadFailed = true;
      toastStore.error(getErrorMessage(err, $t('takedown.mine.loadFailed')));
    } finally {
      loading = false;
    }
  });

  function dispute(item: OwnerTakedown): string {
    if (item.dispute_to === 'organization') {
      return $t('takedown.mine.disputeOrganization', { id: item.file_uuid });
    }
    return item.abuse_contact_email
      ? $t('takedown.mine.disputeOperator', { email: item.abuse_contact_email, id: item.file_uuid })
      : $t('takedown.mine.disputeOperatorNoEmail', { id: item.file_uuid });
  }
</script>

<div class="mine">
  <h3 class="section-title">{$t('takedown.mine.title')}</h3>
  <p class="section-description">{$t('takedown.mine.description')}</p>

  {#if loading}
    <div class="state-row"><Spinner size="small" /><span>{$t('common.loading')}</span></div>
  {:else if loadFailed}
    <div class="state-row error">{$t('takedown.mine.loadFailed')}</div>
  {:else if items.length === 0}
    <EmptyState
      title={$t('takedown.mine.emptyTitle')}
      description={$t('takedown.mine.emptyMessage')}
    />
  {:else}
    <ul class="cards">
      {#each items as item (item.link_uuid)}
        <li class="card">
          <div class="card-head">
            <strong>
              {item.file_title ?? $t('takedown.mine.fileRemoved')}
            </strong>
            <TakedownStatusChip status={item.status} />
          </div>
          <dl>
            <dt>{$t('takedown.mine.type')}</dt>
            <dd>{labelFor($t, TAKEDOWN_NOTICE_TYPE_KEYS, item.notice_type)}</dd>
            <dt>{$t('takedown.mine.takenDownOn')}</dt>
            <dd>{new Date(item.quarantined_at).toLocaleString()}</dd>
            {#if item.outcome_at}
              <dt>{$t('takedown.mine.outcomeOn')}</dt>
              <dd>{new Date(item.outcome_at).toLocaleString()}</dd>
            {/if}
            <dt>{$t('takedown.mine.reason')}</dt>
            <dd class="reason">{item.owner_reason}</dd>
          </dl>
          {#if item.status !== 'released'}
            <p class="dispute">{dispute(item)}</p>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .mine {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .state-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem;
    border-radius: 6px;
    background: var(--bg-secondary, rgba(0, 0, 0, 0.03));
    font-size: 0.8125rem;
  }

  .state-row.error {
    color: #dc2626;
  }

  .cards {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .card {
    padding: 0.875rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--bg-color);
  }

  .card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.5rem;
  }

  dl {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.25rem 1rem;
    margin: 0;
    font-size: 0.8125rem;
  }

  dt {
    color: var(--text-secondary, var(--text-color));
  }

  dd {
    margin: 0;
  }

  .reason {
    overflow-wrap: anywhere;
  }

  .dispute {
    margin: 0.75rem 0 0;
    font-size: 0.8125rem;
    overflow-wrap: anywhere;
  }
</style>
