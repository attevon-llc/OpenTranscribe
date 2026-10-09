<script context="module" lang="ts">
  import type { GrantStatus, SupportGrant } from '$lib/api/supportAccess';

  export type ListPerspective = 'staff' | 'approver';
  export type RowAction = 'start' | 'inUse' | 'approve' | 'deny' | 'revoke' | 'viewLog';

  /**
   * Which actions a row offers. Cosmetic: every one is re-checked server-side.
   * Staff can start only an active grant; the tenant decides pending ones and may
   * revoke active ones. The access log is offered on every row a staffer owns and on
   * every non-pending row a tenant sees.
   */
  export function rowActions(
    status: GrantStatus,
    perspective: ListPerspective,
    inUse: boolean
  ): RowAction[] {
    if (perspective === 'staff') {
      if (status === 'active') return [inUse ? 'inUse' : 'start', 'revoke', 'viewLog'];
      if (status === 'pending') return ['revoke', 'viewLog'];
      return ['viewLog'];
    }
    if (status === 'pending') return ['approve', 'deny'];
    if (status === 'active') return ['revoke', 'viewLog'];
    return ['viewLog'];
  }
</script>

<script lang="ts">
  import { SUPPORT_LEVEL_KEYS } from '$lib/i18n/keyMaps';
  import { createEventDispatcher } from 'svelte';
  import { locale, t } from '$stores/locale';
  import Badge from '$components/ui/Badge.svelte';
  import GrantStatusBadge from './GrantStatusBadge.svelte';
  import { durationLabel, grantTargetLabel, userLabel } from '$lib/supportAccess/format';

  export let grants: SupportGrant[] = [];
  export let perspective: ListPerspective;
  /** Visually hidden table caption, translated by the caller. */
  export let caption = '';
  export let activeGrantUuid: string | null = null;
  /** The grant an action is in flight for; its buttons are disabled. */
  export let busyUuid: string | null = null;

  const dispatch = createEventDispatcher<{
    start: SupportGrant;
    approve: SupportGrant;
    deny: SupportGrant;
    revoke: SupportGrant;
    viewLog: SupportGrant;
  }>();

  function when(iso: string | null): string {
    if (!iso) return '';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString($locale);
  }

  const LABEL: Record<Exclude<RowAction, 'inUse'>, string> = {
    start: 'supportAccess.action.activate',
    approve: 'supportAccess.action.approve',
    deny: 'supportAccess.action.deny',
    revoke: 'supportAccess.action.revoke',
    viewLog: 'supportAccess.action.viewLog',
  };
  const BUTTON_CLASS: Record<Exclude<RowAction, 'inUse'>, string> = {
    start: 'btn btn-primary',
    approve: 'btn btn-primary',
    deny: 'btn btn-danger',
    revoke: 'btn btn-danger',
    viewLog: 'btn btn-secondary',
  };
</script>

<div class="table-wrap">
  <table class="grant-table">
    <caption class="sr-only">{caption}</caption>
    <thead>
      <tr>
        <th scope="col">
          {perspective === 'staff' ? $t('supportAccess.col.target') : $t('supportAccess.col.grantee')}
        </th>
        <th scope="col">{$t('supportAccess.col.level')}</th>
        <th scope="col">{$t('supportAccess.col.mode')}</th>
        <th scope="col">{$t('supportAccess.col.reason')}</th>
        <th scope="col">{$t('supportAccess.col.requested')}</th>
        <th scope="col">{$t('supportAccess.col.expires')}</th>
        <th scope="col">{$t('supportAccess.col.status')}</th>
        <th scope="col" class="actions-col">{$t('supportAccess.col.actions')}</th>
      </tr>
    </thead>
    <tbody>
      {#each grants as grant (grant.uuid)}
        {@const target = grantTargetLabel(grant, $t)}
        {@const name = perspective === 'staff' ? target : userLabel(grant.grantee, $t)}
        <tr class:break-glass={grant.grant_mode === 'break_glass'}>
          <td class="who">
            <span class="primary">{name}</span>
            {#if perspective === 'approver' && grant.grantee?.full_name}
              <span class="secondary">{grant.grantee.email}</span>
            {/if}
          </td>
          <td>{$t(SUPPORT_LEVEL_KEYS[grant.access_level])}</td>
          <td>
            {#if grant.grant_mode === 'break_glass'}
              <Badge variant="error">{$t('supportAccess.mode.break_glass')}</Badge>
              {#if grant.ticket_ref}
                <span class="secondary">{$t('supportAccess.col.ticket')}: <bdi>{grant.ticket_ref}</bdi></span>
              {/if}
            {:else}
              {$t('supportAccess.mode.approved')}
            {/if}
          </td>
          <td class="reason">{grant.reason}</td>
          <td class="when">
            <bdi>{when(grant.requested_at)}</bdi>
            <span class="secondary">{durationLabel(grant.requested_duration_minutes, $t)}</span>
          </td>
          <td class="when"><bdi>{when(grant.expires_at)}</bdi></td>
          <td><GrantStatusBadge status={grant.status} /></td>
          <td class="actions-col">
            {#each rowActions(grant.status, perspective, grant.uuid === activeGrantUuid) as action (action)}
              {#if action === 'inUse'}
                <Badge variant="info">{$t('supportAccess.status.inUse')}</Badge>
              {:else}
                <button
                  type="button"
                  class={`${BUTTON_CLASS[action]} btn-row`}
                  disabled={busyUuid === grant.uuid}
                  aria-label={`${$t(LABEL[action])} ${name}`}
                  on:click={() => dispatch(action, grant)}
                >
                  {$t(LABEL[action])}
                </button>
              {/if}
            {/each}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
  .table-wrap {
    overflow-x: auto;
  }
  .grant-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }
  .grant-table th {
    text-align: start;
    padding: 0.4rem 0.6rem;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-color);
    white-space: nowrap;
  }
  .grant-table td {
    padding: 0.55rem 0.6rem;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
    vertical-align: top;
  }
  tr.break-glass td:first-child {
    border-inline-start: 3px solid var(--error-color);
  }
  .who .primary {
    display: block;
    font-weight: 500;
    word-break: break-word;
  }
  .secondary {
    display: block;
    font-size: 0.75rem;
    color: var(--text-secondary);
    word-break: break-word;
  }
  .reason {
    max-width: 18rem;
    overflow-wrap: anywhere;
  }
  .when {
    white-space: nowrap;
  }
  .actions-col {
    text-align: end;
    white-space: nowrap;
  }
  .btn-row {
    padding: 0.3rem 0.7rem;
    font-size: 0.75rem;
    margin-inline-start: 0.35rem;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  @media (max-width: 768px) {
    .btn-row {
      min-height: 44px;
    }
  }
</style>
