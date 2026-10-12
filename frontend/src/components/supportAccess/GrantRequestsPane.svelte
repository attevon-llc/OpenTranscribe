<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    SupportAccessApi,
    type GrantStatus,
    type SupportGrant,
    type TenantPerspective,
  } from '$lib/api/supportAccess';
  import { SUPPORT_STATUS_KEYS } from '$lib/i18n/keyMaps';
  import { supportGrantErrorCode } from '$lib/supportAccess/errors';
  import { SUPPORT_ACCESS_EVENT } from '$lib/supportAccess/events';
  import { grantTargetLabel, userLabel } from '$lib/supportAccess/format';
  import Spinner from '$components/ui/Spinner.svelte';
  import GrantList from './GrantList.svelte';
  import ApproveGrantModal from './ApproveGrantModal.svelte';
  import GrantDecisionNoteModal from './GrantDecisionNoteModal.svelte';
  import GrantUsesModal from './GrantUsesModal.svelte';

  /**
   * One tenant's view of support access: requests waiting for a decision, then history.
   * `perspective` picks the routes (`workspace` = my personal workspace, `org` = my
   * organization as its admin); the grant shape and the rules are identical.
   */
  export let perspective: TenantPerspective;

  const dispatch = createEventDispatcher<{ countchange: number }>();
  const PENDING_LIMIT = 100;
  const PAGE = 25;
  const FILTERS: GrantStatus[] = ['active', 'denied', 'expired', 'revoked', 'lapsed'];

  let pending: SupportGrant[] = [];
  let pendingTotal = 0;
  let history: SupportGrant[] = [];
  // How many rows the server has handed us for the current filter, including pending ones
  // we hide from "All": the next page starts after them, not after what is shown.
  let historyConsumed = 0;
  let historyTotal = 0;
  let filter: '' | GrantStatus = '';
  let loading = false;
  let loaded = false;
  let failed = false;
  let busyUuid: string | null = null;

  let approving: SupportGrant | null = null;
  let noting: { grant: SupportGrant; mode: 'deny' | 'revoke' } | null = null;
  let logGrant: SupportGrant | null = null;
  let logOpen = false;

  async function loadPending() {
    const page = await SupportAccessApi.listRequests(perspective, {
      status: 'pending',
      limit: PENDING_LIMIT,
      offset: 0,
    });
    pending = page.items;
    pendingTotal = page.total;
    dispatch('countchange', page.total);
  }

  async function loadHistory(append: boolean) {
    const offset = append ? historyConsumed : 0;
    const page = await SupportAccessApi.listRequests(perspective, {
      status: filter || undefined,
      limit: PAGE,
      offset,
    });
    const rows = page.items.filter((g) => g.status !== 'pending');
    history = append ? [...history, ...rows] : rows;
    historyConsumed = offset + page.items.length;
    historyTotal = page.total;
  }

  async function reload(quiet = false) {
    if (!quiet) loading = true;
    try {
      await Promise.all([loadPending(), loadHistory(false)]);
      failed = false;
    } catch (err: unknown) {
      if (!quiet) {
        failed = true;
        toastStore.error(getErrorMessage(err, $t('supportAccess.loadFailed')));
      }
    } finally {
      loading = false;
      loaded = true;
    }
  }

  async function more() {
    loading = true;
    try {
      await loadHistory(true);
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('supportAccess.loadFailed')));
    } finally {
      loading = false;
    }
  }

  /** A 409/403 on a decision means someone else decided first or the rule forbids it; never retry. */
  function reportDecisionError(err: unknown, fallbackKey: string) {
    const code = supportGrantErrorCode(err);
    if (code === 'support_grant_already_decided' || code === 'support_grant_lapsed') {
      toastStore.error($t('supportAccess.alreadyDecided'));
    } else if (code === 'support_grant_self_approval') {
      toastStore.error($t('supportAccess.selfApproval'));
    } else {
      toastStore.error(getErrorMessage(err, $t(fallbackKey)));
    }
  }

  async function approve(event: CustomEvent<{ duration_minutes: number }>) {
    const grant = approving;
    if (!grant) return;
    busyUuid = grant.uuid;
    try {
      await SupportAccessApi.approve(perspective, grant.uuid, event.detail);
      toastStore.success($t('supportAccess.approve.approved'));
      approving = null;
    } catch (err: unknown) {
      reportDecisionError(err, 'supportAccess.approve.failed');
      approving = null;
    } finally {
      busyUuid = null;
      await reload(true);
    }
  }

  async function decideWithNote(event: CustomEvent<{ note?: string }>) {
    const target = noting;
    if (!target) return;
    busyUuid = target.grant.uuid;
    try {
      if (target.mode === 'deny') {
        await SupportAccessApi.deny(perspective, target.grant.uuid, event.detail);
        toastStore.success($t('supportAccess.deny.denied'));
      } else {
        await SupportAccessApi.revokeGrant(target.grant.uuid, event.detail);
        toastStore.success($t('supportAccess.revoke.revoked'));
      }
      noting = null;
    } catch (err: unknown) {
      reportDecisionError(
        err,
        target.mode === 'deny' ? 'supportAccess.deny.failed' : 'supportAccess.revoke.failed'
      );
      noting = null;
    } finally {
      busyUuid = null;
      await reload(true);
    }
  }

  const onPush = () => void reload(true);
  onMount(() => {
    void reload();
    window.addEventListener(SUPPORT_ACCESS_EVENT, onPush);
  });
  onDestroy(() => {
    if (typeof window !== 'undefined') window.removeEventListener(SUPPORT_ACCESS_EVENT, onPush);
  });
</script>

{#if loading && !loaded}
  <div class="state"><Spinner /></div>
{:else if failed}
  <p class="state error" role="alert">{$t('supportAccess.loadFailed')}</p>
{:else}
  <section class="block" aria-labelledby="pending-heading-{perspective}">
    <h4 id="pending-heading-{perspective}">
      {$t('supportAccess.section.pending')}
      {#if pendingTotal > 0}<span class="count">{pendingTotal}</span>{/if}
    </h4>
    {#if pending.length === 0}
      <p class="state">{$t('supportAccess.empty.pending')}</p>
    {:else}
      <GrantList
        grants={pending}
        perspective="approver"
        caption={$t('supportAccess.section.pending')}
        {busyUuid}
        on:approve={(e) => (approving = e.detail)}
        on:deny={(e) => (noting = { grant: e.detail, mode: 'deny' })}
      />
    {/if}
  </section>

  <section class="block" aria-labelledby="history-heading-{perspective}">
    <div class="history-head">
      <h4 id="history-heading-{perspective}">{$t('supportAccess.section.history')}</h4>
      <label class="filter">
        <span>{$t('supportAccess.filter.status')}</span>
        <select bind:value={filter} on:change={() => reload(true)}>
          <option value="">{$t('supportAccess.filter.all')}</option>
          {#each FILTERS as status (status)}
            <option value={status}>{$t(SUPPORT_STATUS_KEYS[status])}</option>
          {/each}
        </select>
      </label>
    </div>
    {#if history.length === 0}
      <p class="state">{$t('supportAccess.empty.history')}</p>
    {:else}
      <GrantList
        grants={history}
        perspective="approver"
        caption={$t('supportAccess.section.history')}
        {busyUuid}
        on:revoke={(e) => (noting = { grant: e.detail, mode: 'revoke' })}
        on:viewLog={(e) => {
          logGrant = e.detail;
          logOpen = true;
        }}
      />
    {/if}
    {#if historyConsumed < historyTotal}
      <button type="button" class="btn btn-secondary more" disabled={loading} on:click={more}>
        {$t('supportAccess.loadMore')}
      </button>
    {/if}
  </section>
{/if}

<ApproveGrantModal
  isOpen={approving !== null}
  grant={approving}
  busy={busyUuid !== null}
  onClose={() => (approving = null)}
  on:submit={approve}
/>
<GrantDecisionNoteModal
  isOpen={noting !== null}
  mode={noting?.mode ?? 'deny'}
  name={noting ? userLabel(noting.grant.grantee, $t) || grantTargetLabel(noting.grant, $t) : ''}
  busy={busyUuid !== null}
  onClose={() => (noting = null)}
  on:submit={decideWithNote}
/>
<GrantUsesModal isOpen={logOpen} grant={logGrant} {perspective} onClose={() => (logOpen = false)} />

<style>
  .block {
    margin-bottom: 1.5rem;
  }
  h4 {
    margin: 0 0 0.5rem 0;
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-color);
  }
  .count {
    margin-inline-start: 0.4rem;
    padding: 0 0.45rem;
    border-radius: 10px;
    background: var(--warning-bg);
    color: var(--text-color);
    font-size: 0.75rem;
  }
  .history-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }
  .filter {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }
  select {
    padding: 0.3rem 0.5rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.8125rem;
  }
  select:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
  .state {
    margin: 1rem 0;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.875rem;
  }
  .state.error {
    color: var(--error-color);
  }
  .more {
    display: block;
    margin: 1rem auto 0 auto;
  }
</style>
