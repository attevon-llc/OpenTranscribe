<script lang="ts">
  import './support-access.css';
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
  function resetOnOpen(open: boolean) {
    if (open === wasOpen) return;
    wasOpen = open;
    if (open) note = '';
  }

  $: resetOnOpen(isOpen);

  function submit() {
    const trimmed = note.trim();
    dispatch('submit', trimmed ? { note: trimmed } : {});
  }
</script>

<BaseModal
  {isOpen}
  {onClose}
  title={mode === 'deny' ? $t('supportAccess.deny.title') : $t('supportAccess.revoke.title')}
  size="sm"
>
  {#if mode === 'revoke'}
    <p class="support-modal-lead">{$t('supportAccess.revoke.body', { name })}</p>
  {/if}
  <form id="grant-note-form" on:submit|preventDefault={submit}>
    <label for="grant-note" class="support-field-label">{$t('supportAccess.note.label')}</label>
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
  textarea {
    width: 100%;
    resize: vertical;
  }
</style>
