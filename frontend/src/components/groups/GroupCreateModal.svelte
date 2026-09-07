<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { Group } from '$lib/types/groups';
  import { GroupsApi } from '$lib/api/groups';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import BaseModal from '../ui/BaseModal.svelte';

  export let isOpen = false;
  // Kept for callers that pass noBackdrop (not used by BaseModal)
  export let noBackdrop = false;
  $: { noBackdrop; }

  const dispatch = createEventDispatcher<{
    created: Group;
    close: void;
  }>();

  let name = '';
  let description = '';
  let isCreating = false;

  function resetForm() {
    name = '';
    description = '';
    isCreating = false;
  }

  function handleClose() {
    resetForm();
    isOpen = false;
    dispatch('close');
  }

  async function handleSubmit() {
    if (!name.trim()) return;
    isCreating = true;

    try {
      const group = await GroupsApi.createGroup({
        name: name.trim(),
        description: description.trim() || undefined,
      });

      toastStore.success($t('groups.toast.groupCreated'));
      dispatch('created', group);
      handleClose();
    } catch (err: unknown) {
      const message = getErrorMessage(err, $t('groups.toast.createGroupFailed'));
      toastStore.error(message);
    } finally {
      isCreating = false;
    }
  }
</script>

<BaseModal {isOpen} title={$t('groups.createGroup')} maxWidth="500px" zIndex={1300} onClose={handleClose}>
  <form id="group-create-form" on:submit|preventDefault={handleSubmit}>
    <div class="modal-body">
      <div class="form-group">
        <label for="group-name">{$t('groups.groupName')}</label>
        <input
          type="text"
          id="group-name"
          class="form-control"
          bind:value={name}
          maxlength="255"
          placeholder={$t('groups.groupNamePlaceholder')}
          required
        />
      </div>

      <div class="form-group">
        <label for="group-description">{$t('groups.description')}</label>
        <textarea
          id="group-description"
          class="form-control form-textarea"
          bind:value={description}
          rows="3"
          placeholder={$t('groups.descriptionPlaceholder')}
        ></textarea>
        <span class="form-hint">{$t('groups.descriptionHint')}</span>
      </div>
    </div>
  </form>

  <svelte:fragment slot="footer">
    <button
      type="button"
      class="modal-button cancel-button"
      on:click={handleClose}
    >
      {$t('modal.cancel')}
    </button>
    <button
      type="submit"
      form="group-create-form"
      class="modal-button confirm-button"
      disabled={!name.trim() || isCreating}
    >
      {isCreating ? $t('groups.creating') : $t('groups.createGroup')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .modal-body {
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .form-group label {
    font-weight: 500;
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  .form-control {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    transition: border-color 0.15s, box-shadow 0.15s;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px var(--primary-light);
  }

  .form-control::placeholder {
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .form-textarea {
    resize: vertical;
    min-height: 60px;
    font-family: inherit;
  }

  .modal-button {
    padding: 0.6rem 1.2rem;
    border: none;
    border-radius: 8px;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .cancel-button {
    background: var(--card-background);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    box-shadow: var(--card-shadow);
  }

  .cancel-button:hover {
    background: var(--button-hover, #e5e7eb);
    border-color: var(--border-color);
  }

  .confirm-button {
    background: var(--primary-color);
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .confirm-button:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .confirm-button:active:not(:disabled) {
    transform: scale(1);
  }

  .confirm-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
  }

  .form-hint {
    display: block;
    margin-top: 0.35rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-style: italic;
  }

  @media (max-width: 768px) {
    .modal-button {
      min-height: 44px;
      width: 100%;
    }

    .form-control {
      min-height: 44px;
      font-size: 1rem;
    }
  }

</style>
