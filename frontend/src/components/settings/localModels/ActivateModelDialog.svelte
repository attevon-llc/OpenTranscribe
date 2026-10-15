<!--
  ActivateModelDialog.svelte - the explicit confirmation before the transcription worker is
  restarted to run another model (issue #1246). Wording lives in settings.localModels.activateDialog.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { t } from '$stores/locale';

  export let isOpen = false;
  export let model = '';

  const dispatch = createEventDispatcher<{ confirm: void; cancel: void }>();
</script>

<ConfirmationModal
  {isOpen}
  title={$t('settings.localModels.activateDialog.title')}
  message={$t('settings.localModels.activateDialog.message', { model })}
  confirmText={$t('settings.localModels.activateDialog.confirm')}
  confirmButtonClass="confirm-button"
  on:confirm={() => dispatch('confirm')}
  on:cancel={() => dispatch('cancel')}
  on:close={() => dispatch('cancel')}
/>
