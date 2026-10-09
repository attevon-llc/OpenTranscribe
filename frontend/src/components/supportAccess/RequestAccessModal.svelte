<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import GrantTargetFields, { type TargetChoice } from './GrantTargetFields.svelte';
  import {
    SupportAccessApi,
    type AccessLevel,
    type SupportGrant,
    type TargetKind,
  } from '$lib/api/supportAccess';
  import { getErrorMessage } from '$lib/utils/apiError';

  export let isOpen = false;
  export let onClose: () => void = () => {};

  const dispatch = createEventDispatcher<{ created: SupportGrant }>();

  // UX hint only: the server owns the real 10..2000 validation and its 422 is shown below.
  const REASON_MIN = 10;

  let targetKind: TargetKind = 'organization';
  let target: TargetChoice | null = null;
  let level: AccessLevel = 'read';
  let duration = 60;
  let reason = '';
  let submitting = false;
  let error = '';

  $: canSubmit = !!target && reason.trim().length >= REASON_MIN && !submitting;

  function reset() {
    targetKind = 'organization';
    target = null;
    level = 'read';
    duration = 60;
    reason = '';
    error = '';
  }

  // Start clean each time it opens; a half-typed reason from last time is a leak of intent.
  let wasOpen = false;
  $: if (isOpen !== wasOpen) {
    wasOpen = isOpen;
    if (isOpen) reset();
  }

  async function submit() {
    if (!target || !canSubmit) return;
    submitting = true;
    error = '';
    try {
      const grant = await SupportAccessApi.requestGrant({
        organization_uuid: targetKind === 'organization' ? target.uuid : null,
        subject_user_uuid: targetKind === 'personal' ? target.uuid : null,
        access_level: level,
        reason: reason.trim(),
        duration_minutes: duration,
      });
      toastStore.success($t('supportAccess.request.sent'));
      dispatch('created', grant);
      onClose();
    } catch (err: unknown) {
      error = getErrorMessage(err, $t('supportAccess.request.failed'));
    } finally {
      submitting = false;
    }
  }
</script>

<BaseModal {isOpen} {onClose} title={$t('supportAccess.request.title')} maxWidth="560px" allowOverflow>
  <form id="support-request-form" on:submit|preventDefault={submit}>
    <GrantTargetFields
      idPrefix="request"
      bind:targetKind
      bind:target
      bind:level
      bind:duration
      bind:reason
    />
    {#if error}
      <p class="error" role="alert">{error}</p>
    {/if}
  </form>
  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-secondary" on:click={onClose}>
      {$t('common.cancel')}
    </button>
    <button type="submit" form="support-request-form" class="btn btn-primary" disabled={!canSubmit}>
      {$t('supportAccess.request.submit')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .error {
    margin: 0;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--error-border);
    border-radius: 6px;
    background: var(--error-background);
    color: var(--error-color);
    font-size: 0.8125rem;
  }
</style>
