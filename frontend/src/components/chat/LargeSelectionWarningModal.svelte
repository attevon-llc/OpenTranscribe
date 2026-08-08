<!--
  LargeSelectionWarningModal.svelte — confirm an oversized context selection.

  Selecting more recordings than the model's context can hold does not fail; it
  degrades quietly, because retrieval simply picks fewer passages from each file
  and answers get thinner. That silent degradation is exactly what needs an
  explicit confirmation — the user should know they are trading depth for
  breadth, not discover it from a vague answer.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import type { ContextEstimate } from '$lib/types/chat';

  export let isOpen = false;
  export let estimate: ContextEstimate | null = null;

  const dispatch = createEventDispatcher<{ proceed: void; cancel: void }>();
</script>

<BaseModal
  {isOpen}
  title={$t('chat.warnings.largeSelectionTitle')}
  maxWidth="460px"
  onClose={() => dispatch('cancel')}
>
  <div class="warning-body" data-testid="chat-large-selection-warning">
    <p>
      {$t('chat.warnings.largeSelectionMessage', {
        files: estimate?.file_count ?? 0,
        pct: Math.round(estimate?.pct ?? 0),
      })}
    </p>
    <p class="hint">{$t('chat.warnings.largeSelectionHint')}</p>

    <div class="warning-actions">
      <button
        type="button"
        class="modal-button modal-cancel-button"
        on:click={() => dispatch('cancel')}
      >
        {$t('common.cancel')}
      </button>
      <button
        type="button"
        class="modal-button modal-primary-button"
        on:click={() => dispatch('proceed')}
        data-testid="chat-large-selection-proceed"
      >
        {$t('chat.warnings.proceed')}
      </button>
    </div>
  </div>
</BaseModal>

<style>
  .warning-body {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  p {
    margin: 0;
    font-size: 0.9rem;
    line-height: 1.55;
    color: var(--text-color);
  }

  .hint {
    font-size: 0.82rem;
    color: var(--text-secondary);
  }

  .warning-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }
</style>
