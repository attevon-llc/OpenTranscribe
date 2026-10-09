<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';

  /**
   * Deny a request or revoke access, with an optional note. The note goes to the audit
   * event only (never the grant row), so the copy says nothing about the other party
   * reading it. The parent runs the call, because it also has to handle the 409s.
   */
  export let isOpen = false;
  export let mode: 'deny' | 'revoke' = 'deny';
  /** Who the action concerns; shown in the revoke body. */
  export let name = '';
  export let busy = false;
  export let onClose: () => void = () => {};

  const dispatch = createEventDispatcher<{ submit: { note?: string } }>();
  const NOTE_MAX = 500;
  let note = '';

  let wasOpen = false;
  $: if (isOpen !== wasOpen) {
    wasOpen = isOpen;
    if (isOpen) note = '';
  }

  function submit() {
    const trimmed = note.trim();
    dispatch('submit', trimmed ? { note: trimmed } : {});
  }
</script>

<BaseModal
  {isOpen}
  {onClose}
  title={mode === 'deny' ? $t('supportAccess.deny.title') : $t('supportAccess.revoke.title')}
  maxWidth="480px"
>
  {#if mode === 'revoke'}
    <p class="body">{$t('supportAccess.revoke.body', { name })}</p>
  {/if}
  <form id="grant-note-form" on:submit|preventDefault={submit}>
    <label for="grant-note" class="label">{$t('supportAccess.note.label')}</label>
    <textarea id="grant-note" rows="3" maxlength={NOTE_MAX} bind:value={note}></textarea>
  </form>
  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-secondary" on:click={onClose}>
      {$t('common.cancel')}
    </button>
    <button type="submit" form="grant-note-form" class="btn btn-danger" disabled={busy}>
      {mode === 'deny' ? $t('supportAccess.action.deny') : $t('supportAccess.action.revoke')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .body {
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
  textarea {
    width: 100%;
    padding: 0.5rem 0.75rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 0.875rem;
    font-family: inherit;
    resize: vertical;
  }
  textarea:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }
</style>
