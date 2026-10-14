<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { SUPPORT_LEVEL_KEYS } from '$lib/i18n/keyMaps';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import type { SupportGrant } from '$lib/api/supportAccess';
  import { durationLabel, userLabel } from '$lib/supportAccess/format';

  /**
   * Approve a pending request. The approver may SHORTEN the requested duration, never
   * extend it, so the select offers nothing above what was asked for (the server refuses
   * it too, with a 422).
   */
  export let isOpen = false;
  export let grant: SupportGrant | null = null;
  export let busy = false;
  export let onClose: () => void = () => {};

  const dispatch = createEventDispatcher<{ submit: { duration_minutes: number } }>();
  const STEPS = [15, 30, 60, 120, 240, 480];

  let duration = 0;
  let forGrant: string | null = null;

  $: requested = grant?.requested_duration_minutes ?? 0;
  // The standard steps up to the request, plus the request itself when it is not a standard step.
  $: choices = [...new Set([...STEPS.filter((m) => m <= requested), requested])]
    .filter((m) => m >= 15)
    .sort((a, b) => a - b);
  $: if (isOpen && grant && forGrant !== grant.uuid) {
    forGrant = grant.uuid;
    duration = requested;
  }
  $: if (!isOpen) forGrant = null;
</script>

<BaseModal {isOpen} {onClose} title={$t('supportAccess.approve.title')} maxWidth="500px">
  {#if grant}
    <dl class="facts">
      <dt>{$t('supportAccess.col.grantee')}</dt>
      <dd>{userLabel(grant.grantee, $t)}</dd>
      <dt>{$t('supportAccess.col.level')}</dt>
      <dd>{$t(SUPPORT_LEVEL_KEYS[grant.access_level])}</dd>
      <dt>{$t('supportAccess.col.reason')}</dt>
      <dd class="reason">{grant.reason}</dd>
    </dl>
    <p class="requested">
      {$t('supportAccess.approve.requestedDuration', {
        duration: durationLabel(requested, $t),
      })}
    </p>
    <form id="approve-form" on:submit|preventDefault={() => dispatch('submit', { duration_minutes: duration })}>
      <label for="approve-duration" class="label">{$t('supportAccess.approve.grantFor')}</label>
      <select id="approve-duration" bind:value={duration}>
        {#each choices as minutes (minutes)}
          <option value={minutes}>{durationLabel(minutes, $t)}</option>
        {/each}
      </select>
      <p class="hint">{$t('supportAccess.approve.shortenOnly')}</p>
    </form>
  {/if}
  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-secondary" on:click={onClose}>
      {$t('common.cancel')}
    </button>
    <button type="submit" form="approve-form" class="btn btn-primary" disabled={busy || !grant}>
      {$t('supportAccess.action.approve')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .facts {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.35rem 1rem;
    margin: 0 0 1rem 0;
    font-size: 0.875rem;
  }
  dt {
    font-weight: 600;
    color: var(--text-secondary);
  }
  dd {
    margin: 0;
    color: var(--text-color);
    overflow-wrap: anywhere;
  }
  .requested {
    margin: 0 0 1rem 0;
    font-size: 0.875rem;
    color: var(--text-color);
  }
  .label {
    display: block;
    margin-bottom: 0.35rem;
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-color);
  }
  select {
    width: 100%;
    padding: 0.5rem 0.75rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.875rem;
  }
  select:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
  .hint {
    margin: 0.35rem 0 0 0;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }
</style>
