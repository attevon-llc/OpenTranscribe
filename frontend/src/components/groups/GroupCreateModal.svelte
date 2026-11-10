<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { createEventDispatcher } from 'svelte';
  import type { Group } from '$lib/types/groups';
  import { GroupsApi } from '$lib/api/groups';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import BaseModal from '$components/ui/BaseModal.svelte';

  export let isOpen = false;

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

<BaseModal {isOpen} title={$t('groups.createGroup')} size="sm" zIndex={1300} onClose={handleClose}>
  <form id="group-create-form" on:submit|preventDefault={handleSubmit}>
    <div class="modal-body">
      <FormField id="group-name" label={$t('groups.groupName')}>
        <input
          type="text"
          id="group-name"
          class="form-control"
          bind:value={name}
          maxlength="255"
          placeholder={$t('groups.groupNamePlaceholder')}
          required
        />
      </FormField>

      <FormField id="group-description" label={$t('groups.description')} hint={$t('groups.descriptionHint')} let:describedby>
        <textarea
          id="group-description" aria-describedby={describedby}
          class="form-control form-textarea"
          bind:value={description}
          rows="3"
          placeholder={$t('groups.descriptionPlaceholder')}
        ></textarea>
      </FormField>
    </div>
  </form>

  <svelte:fragment slot="footer">
    <button
      type="button"
      class="btn btn-lg btn-secondary"
      on:click={handleClose}
    >
      {$t('modal.cancel')}
    </button>
    <button
      type="submit"
      form="group-create-form"
      class="btn btn-lg btn-primary"
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
    gap: var(--space-4);
  }



  .form-control {
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-sm);
  }

  .form-control::placeholder {
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .form-textarea {
    resize: vertical;
    min-height: 60px;
  }


  @media (max-width: 768px) {
    .btn-lg {
      min-height: 44px;
      width: 100%;
    }

    .form-control {
      min-height: 44px;
      font-size: var(--font-size-base);
    }
  }

</style>
