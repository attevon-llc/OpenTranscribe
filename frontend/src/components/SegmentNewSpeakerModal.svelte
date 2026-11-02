<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { portal } from '$lib/actions/portal';
  import { newSpeakerNameState } from '$lib/speakers/speakerMenu';
  import type { Speaker } from '$lib/types/speaker';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import BaseModal from '$components/ui/BaseModal.svelte';

  export let open: boolean = false;
  export let mediaFileUuid: string = '';
  /** The next free diarization slot (e.g. SPEAKER_03); kept as `name`, the human label is `display_name`. */
  export let speakerSlot: string = '';

  // `created` carries the new speaker (the parent forwards it and selects it for the segment);
  // `close` fires on every way out, so the parent owns whether the dialog is shown.
  const dispatch = createEventDispatcher<{ created: { speaker: Speaker }; close: void }>();

  let name = '';
  let isCreating = false;

  $: ({ isPlaceholder, canCreate } = newSpeakerNameState(name, isCreating));

  function close() {
    name = '';
    dispatch('close');
  }

  async function confirmCreate() {
    const typedName = name.trim();
    if (!mediaFileUuid || !canCreate) return;

    isCreating = true;
    try {
      // `name` keeps the diarization slot (speaker colours hash it, and the
      // named-vs-auto ordering reads it); the human's label goes in
      // `display_name`, which is also what marks the new row verified server-side.
      const response = await axiosInstance.post(`/speakers?media_file_uuid=${mediaFileUuid}`, {
        name: speakerSlot,
        display_name: typedName
      });

      dispatch('created', { speaker: response.data });
      close();
    } catch (error) {
      console.error('Failed to create new speaker:', error);
      toastStore.error($t('common.somethingWentWrong'));
    } finally {
      isCreating = false;
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      confirmCreate();
    }
  }
</script>

<!--
  Portaled to <body> deliberately. This component renders inside a transcript
  segment's `<button class="segment-content">`, so an in-place dialog is both
  invalid HTML (interactive content inside a button) and visually broken: the
  backdrop's `position: fixed` resolved `width/height: 100%` against the 548px
  segment row, not the viewport, so the dialog rendered as a strip wedged into
  the transcript row with no backdrop and no way to use it (#740).

  `closeOnBackdropClick={false}` because the dialog holds a half-typed name.
-->
<div use:portal>
  <BaseModal
    isOpen={open}
    title={$t('speaker.newSpeakerTitle')}
    size="sm"
    closeOnBackdropClick={false}
    onClose={close}
  >
    <label class="new-speaker-field">
      <span class="new-speaker-label">{$t('speaker.newSpeakerNameLabel')}</span>
      <!-- svelte-ignore a11y-autofocus -->
      <input
        class="new-speaker-name-input"
        type="text"
        autofocus
        maxlength="100"
        bind:value={name}
        on:keydown={handleKeydown}
        placeholder={$t('speaker.newSpeakerNamePlaceholder')}
      />
    </label>
    {#if isPlaceholder}
      <p class="new-speaker-error">{$t('speaker.placeholderNameRejected')}</p>
    {:else}
      <p class="new-speaker-hint">{$t('speaker.newSpeakerSlotHint', { label: speakerSlot })}</p>
    {/if}
    <svelte:fragment slot="footer">
      <!-- `modal-button` / `modal-cancel-button` / `modal-primary-button` are the
         global classes in `src/styles/form-elements.css`; the `create-speaker-*`
         classes are selector hooks only and carry no styling of their own. -->
      <button
        type="button"
        class="modal-button modal-cancel-button create-speaker-cancel"
        on:click={close}
      >
        {$t('common.cancel')}
      </button>
      <button
        type="button"
        class="modal-button modal-primary-button create-speaker-confirm"
        on:click={confirmCreate}
        disabled={!canCreate}
      >
        {isCreating ? $t('common.saving') : $t('speaker.addSpeaker')}
      </button>
    </svelte:fragment>
  </BaseModal>
</div>

<style>
  .new-speaker-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .new-speaker-label {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-semibold);
    color: var(--text-secondary);
  }

  /* The input itself is deliberately unstyled here: `src/styles/form-elements.css`
     already themes every `input` for light and dark. */

  .new-speaker-hint {
    margin: var(--space-2-5) 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .new-speaker-error {
    margin: var(--space-2-5) 0 0;
    font-size: var(--font-size-xs);
    color: var(--error-color);
  }
</style>
