<script lang="ts">
  /**
   * Confirmation dialog asking whether a renamed speaker should update its
   * global profile or become a new one.
   *
   * The page owns visibility, so this component is rendered inside the page's
   * `{#if}` and only reports the chosen action.
   */
  import { createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import { t } from '$stores/locale';

  export let title = '';
  export let message = '';

  const dispatch = createEventDispatcher();
</script>

<BaseModal isOpen={true} {title} size="sm" onClose={() => dispatch('cancel')}>
  <p class="modal-message">{message}</p>

  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-secondary" on:click={() => dispatch('cancel')}>
      {$t('common.cancel')}
    </button>
    <button type="button" class="btn btn-secondary" on:click={() => dispatch('createNewProfile')}>
      {$t('speakerProfile.createNew')}
    </button>
    <button type="button" class="btn btn-primary" on:click={() => dispatch('updateProfile')}>
      {$t('speakerProfile.updateGlobally')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .modal-message {
    margin: 0;
    color: var(--text-secondary);
    line-height: 1.5;
    font-size: 0.95rem;
  }
</style>
