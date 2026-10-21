<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  export let validationError = '';
  export let saving = false;
  export let resetting = false;
  export let dirty = false;

  const dispatch = createEventDispatcher<{ reset: void; save: void }>();
</script>

{#if validationError}
  <div class="validation-error">
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10"></circle>
      <line x1="12" y1="8" x2="12" y2="12"></line>
      <line x1="12" y1="16" x2="12.01" y2="16"></line>
    </svg>
    <span>{validationError}</span>
  </div>
{/if}

<div class="button-row">
  <button
    type="button"
    class="btn btn-secondary"
    on:click={() => dispatch('reset')}
    disabled={saving || resetting}
  >
    {resetting ? $t('settings.transcription.resetting') : $t('settings.transcription.resetToDefaults')}
  </button>
  <button
    type="button"
    class="btn btn-primary"
    on:click={() => dispatch('save')}
    disabled={saving || resetting || !dirty || !!validationError}
  >
    {saving ? $t('settings.transcription.saving') : $t('settings.transcription.saveSettings')}
  </button>
</div>
