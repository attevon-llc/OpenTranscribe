<script lang="ts">
  import { focusOnMount } from '$lib/actions/focusOnMount';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';

  export let isOpen = false;
  export let name = '';

  const dispatch = createEventDispatcher<{ confirm: void; close: void }>();
</script>

<!-- Promote Modal (inline with text input) -->
<BaseModal
  {isOpen}
  title={$t('speakers.promote.title')}
  size="sm"
  onClose={() => dispatch('close')}
>
  <p class="promote-description">{$t('speakers.promote.description')}</p>
  <input
    class="promote-modal-input"
    bind:value={name}
    placeholder={$t('speakers.promote.namePlaceholder')}
    aria-label={$t('speakers.promote.title')}
    on:keydown={(e) => { if (e.key === 'Enter') dispatch('confirm'); }}
    use:focusOnMount
  />
  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-secondary" on:click={() => dispatch('close')}>{$t('modal.cancel')}</button>
    <button type="button" class="btn btn-primary" on:click={() => dispatch('confirm')} disabled={!name.trim()}>{$t('speakers.promote.confirm')}</button>
  </svelte:fragment>
</BaseModal>

<style>
  /* Promote modal: chrome comes from ui/BaseModal. */
  .promote-description {
    margin: 0 0 var(--space-4);
    font-size: var(--font-size-md);
    color: var(--text-secondary);
  }
</style>
