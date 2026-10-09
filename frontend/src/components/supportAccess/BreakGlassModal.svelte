<script lang="ts">
  import { SUPPORT_LEVEL_KEYS } from '$lib/i18n/keyMaps';
  import { createEventDispatcher, tick } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import GrantTargetFields, { type TargetChoice } from './GrantTargetFields.svelte';
  import {
    SupportAccessApi,
    type AccessLevel,
    type SupportGrant,
    type TargetKind,
  } from '$lib/api/supportAccess';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { durationLabel } from '$lib/supportAccess/format';

  export let isOpen = false;
  export let onClose: () => void = () => {};

  const dispatch = createEventDispatcher<{ created: SupportGrant; start: SupportGrant }>();

  // Break-glass is capped at 4 hours server-side (15..240); 480 is not offered.
  const DURATIONS = [15, 30, 60, 120, 240];
  const REASON_MIN = 10;

  type Step = 'form' | 'confirm' | 'done';
  let step: Step = 'form';
  let targetKind: TargetKind = 'organization';
  let target: TargetChoice | null = null;
  let level: AccessLevel = 'read';
  let duration = 60;
  let reason = '';
  let ticket = '';
  let typedName = '';
  let submitting = false;
  let error = '';
  let created: SupportGrant | null = null;
  let heading: HTMLElement | undefined;

  $: canReview = !!target && reason.trim().length >= REASON_MIN && ticket.trim().length > 0;
  // Exact, case-sensitive: the typed name is the friction that makes this deliberate.
  $: nameMatches = !!target && typedName === target.label;

  function reset() {
    step = 'form';
    targetKind = 'organization';
    target = null;
    level = 'read';
    duration = 60;
    reason = '';
    ticket = '';
    typedName = '';
    error = '';
    created = null;
  }

  async function go(next: Step) {
    step = next;
    error = '';
    await tick();
    heading?.focus();
  }

  async function confirm() {
    if (!target || !nameMatches || submitting) return;
    submitting = true;
    error = '';
    try {
      created = await SupportAccessApi.breakGlass({
        organization_uuid: targetKind === 'organization' ? target.uuid : null,
        subject_user_uuid: targetKind === 'personal' ? target.uuid : null,
        access_level: level,
        reason: reason.trim(),
        ticket_ref: ticket.trim(),
        duration_minutes: duration,
      });
      dispatch('created', created);
      await go('done');
    } catch (err: unknown) {
      error = getErrorMessage(err, $t('supportAccess.breakGlass.failed'));
    } finally {
      submitting = false;
    }
  }

  function startNow() {
    if (created) dispatch('start', created);
    close();
  }

  function close() {
    // A finished request starts the next one clean; an abandoned draft keeps its values.
    if (step === 'done') reset();
    else {
      step = 'form';
      typedName = '';
      error = '';
    }
    onClose();
  }
</script>

<BaseModal
  {isOpen}
  onClose={close}
  title={$t('supportAccess.breakGlass.title')}
  maxWidth="580px"
  allowOverflow
  closeOnBackdropClick={false}
>
  {#if step === 'form'}
    <h3 class="step" tabindex="-1" bind:this={heading}>{$t('supportAccess.breakGlass.title')}</h3>
    <p class="warning" role="note">{$t('supportAccess.breakGlass.warning')}</p>
    <form id="break-glass-form" on:submit|preventDefault={() => go('confirm')}>
      <GrantTargetFields
        idPrefix="bg"
        durations={DURATIONS}
        bind:targetKind
        bind:target
        bind:level
        bind:duration
        bind:reason
      />
      <p class="max">{$t('supportAccess.breakGlass.maxDuration')}</p>
      <div class="field">
        <label for="bg-ticket" class="label">{$t('supportAccess.breakGlass.ticket')}</label>
        <input
          id="bg-ticket"
          type="text"
          maxlength="255"
          aria-describedby="bg-ticket-hint"
          bind:value={ticket}
        />
        <div id="bg-ticket-hint" class="hint">{$t('supportAccess.breakGlass.ticketHint')}</div>
      </div>
    </form>
  {:else if step === 'confirm' && target}
    <h3 class="step" tabindex="-1" bind:this={heading}>
      {$t('supportAccess.breakGlass.confirmTitle')}
    </h3>
    <p class="warning" role="note">{$t('supportAccess.breakGlass.warning')}</p>
    <p>
      {$t('supportAccess.breakGlass.confirmSummary', {
        level: $t(SUPPORT_LEVEL_KEYS[level]),
        target: target.label,
        duration: durationLabel(duration, $t),
      })}
    </p>
    <form id="break-glass-confirm" on:submit|preventDefault={confirm}>
      <label for="bg-typed" class="label" id="bg-typed-label">
        {$t('supportAccess.breakGlass.confirmTypeName', { target: target.label })}
      </label>
      <input
        id="bg-typed"
        type="text"
        autocomplete="off"
        bind:value={typedName}
      />
      {#if error}
        <p class="error" role="alert">{error}</p>
      {/if}
    </form>
  {:else}
    <h3 class="step" tabindex="-1" bind:this={heading}>
      {$t('supportAccess.breakGlass.created')}
    </h3>
  {/if}

  <svelte:fragment slot="footer">
    {#if step === 'form'}
      <button type="button" class="btn btn-secondary" on:click={close}>
        {$t('common.cancel')}
      </button>
      <button type="submit" form="break-glass-form" class="btn btn-danger" disabled={!canReview}>
        {$t('supportAccess.breakGlass.review')}
      </button>
    {:else if step === 'confirm'}
      <button type="button" class="btn btn-secondary" on:click={() => go('form')}>
        {$t('supportAccess.breakGlass.back')}
      </button>
      <button
        type="submit"
        form="break-glass-confirm"
        class="btn btn-danger"
        aria-describedby="bg-typed-label"
        disabled={!nameMatches || submitting}
      >
        {$t('supportAccess.breakGlass.confirm')}
      </button>
    {:else}
      <button type="button" class="btn btn-secondary" on:click={close}>
        {$t('common.close')}
      </button>
      <button type="button" class="btn btn-primary" on:click={startNow}>
        {$t('supportAccess.breakGlass.startNow')}
      </button>
    {/if}
  </svelte:fragment>
</BaseModal>

<style>
  .step {
    margin: 0 0 0.75rem 0;
    font-size: 1rem;
    color: var(--text-color);
  }
  .step:focus {
    outline: none;
  }
  .step:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
  .warning {
    margin: 0 0 1rem 0;
    padding: 0.65rem 0.85rem;
    border: 1px solid var(--warning-color);
    border-inline-start-width: 4px;
    border-radius: 6px;
    background: var(--warning-bg);
    color: var(--text-color);
    font-size: 0.8125rem;
    line-height: 1.5;
  }
  .max {
    margin: -0.5rem 0 1rem 0;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }
  .field {
    margin-bottom: 1rem;
  }
  .label {
    display: block;
    margin-bottom: 0.35rem;
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-color);
  }
  input[type='text'] {
    width: 100%;
    padding: 0.5rem 0.75rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.875rem;
  }
  input[type='text']:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
  .hint {
    margin-top: 0.25rem;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }
  .error {
    margin: 0.75rem 0 0 0;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--error-border);
    border-radius: 6px;
    background: var(--error-background);
    color: var(--error-color);
    font-size: 0.8125rem;
  }
</style>
