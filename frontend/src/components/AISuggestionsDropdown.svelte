<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';
  import { t } from '$stores/locale';

  export let suggestions: Array<{name: string, confidence: number, rationale?: string}> = [];
  export let type: 'tag' | 'collection' = 'tag';
  export let loading = false;
  export let autoAppliedTags: string[] = [];
  export let autoAppliedCollections: string[] = [];

  const dispatch = createEventDispatcher();

  let isExpanded = false;
  let editingSuggestion: {name: string, confidence: number, rationale?: string} | null = null;
  let editedName = '';

  function toggleExpanded() {
    isExpanded = !isExpanded;
  }

  function getConfidenceColor(confidence: number): string {
    if (confidence >= 0.7) return '#10b981'; // Green
    if (confidence >= 0.5) return '#f59e0b'; // Yellow/Orange
    return '#ef4444'; // Red
  }

  function getConfidenceBackground(confidence: number): string {
    if (confidence >= 0.7) return 'rgba(16, 185, 129, 0.1)'; // Light green
    if (confidence >= 0.5) return 'rgba(245, 158, 11, 0.1)'; // Light yellow
    return 'rgba(239, 68, 68, 0.1)'; // Light red
  }

  function handleAccept(suggestion: any) {
    dispatch('accept', { suggestion });
  }

  function startEdit(suggestion: any) {
    editingSuggestion = suggestion;
    editedName = suggestion.name;
  }

  function cancelEdit() {
    editingSuggestion = null;
    editedName = '';
  }

  function saveEdit() {
    if (!editingSuggestion || !editedName.trim()) return;

    const updatedSuggestion = {
      ...editingSuggestion,
      name: editedName.trim()
    };

    dispatch('accept', { suggestion: updatedSuggestion });
    cancelEdit();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      saveEdit();
    } else if (e.key === 'Escape') {
      cancelEdit();
    }
  }

  $: hasSuggestions = suggestions && suggestions.length > 0;
  $: suggestionLabel = type === 'tag' ? $t('ai.tags') : $t('ai.collections');
</script>

{#if hasSuggestions}
  <div class="ai-suggestions">
    <button
      class="ai-suggestions-header"
      on:click={toggleExpanded}
      disabled={loading}
    >
      <span class="ai-badge">
        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 2L2 7l10 5 10-5-10-5z"></path>
          <path d="M2 17l10 5 10-5M2 12l10 5 10-5"></path>
        </svg>
        {$t('ai.suggestedLabel', { type: suggestionLabel })}
      </span>
      <span class="suggestion-count">{suggestions.length}</span>
      <svg
        class="dropdown-icon"
        xmlns="http://www.w3.org/2000/svg"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        style="transform: rotate({isExpanded ? '180deg' : '0deg'})"
      >
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
    </button>

    {#if isExpanded}
      <div class="ai-suggestions-content" transition:slide={{ duration: 200 }}>
        <!-- Edit Modal (inline, shows when editing) -->
        {#if editingSuggestion}
          <div class="edit-modal" transition:slide={{ duration: 200 }}>
            <div class="edit-header">
              <h4>{type === 'tag' ? $t('ai.editTagName') : $t('ai.editCollectionName')}</h4>
            </div>
            <div class="edit-body">
              <input
                type="text"
                bind:value={editedName}
                on:keydown={handleKeydown}
                placeholder={$t('ai.enterNamePlaceholder')}
                class="edit-input"
              />
            </div>
            <div class="edit-actions">
              <button class="btn-cancel" on:click={cancelEdit}>{$t('ai.cancel')}</button>
              <button class="btn-save" on:click={saveEdit} disabled={!editedName.trim()}>
                {$t('ai.saveAndAdd')}
              </button>
            </div>
          </div>
        {:else}
          <!-- Suggestions as colored chips -->
          <div class="suggestions-chips">
            {#each suggestions as suggestion}
              <div
                class="suggestion-chip"
                style="
                  background: {getConfidenceBackground(suggestion.confidence)};
                  border-color: {getConfidenceColor(suggestion.confidence)};
                "
                title={suggestion.rationale || `${Math.round(suggestion.confidence * 100)}%`}
              >
                <button
                  class="chip-add"
                  on:click={() => handleAccept(suggestion)}
                  disabled={loading}
                  title={$t('ai.addAsIs')}
                >
                  <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <line x1="12" y1="5" x2="12" y2="19"></line>
                    <line x1="5" y1="12" x2="19" y2="12"></line>
                  </svg>
                </button>
                <span class="chip-name">{suggestion.name}</span>
                <button
                  class="chip-edit"
                  on:click={() => startEdit(suggestion)}
                  disabled={loading}
                  title={$t('ai.editBeforeAdding')}
                >
                  <svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                    <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                  </svg>
                </button>
              </div>
            {/each}
          </div>
        {/if}

        <!-- Auto-applied info banner -->
        {#if (type === 'tag' && autoAppliedTags.length > 0) || (type === 'collection' && autoAppliedCollections.length > 0)}
          <div class="auto-applied-banner">
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor" class="sparkle-icon">
              <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
            </svg>
            <span>
              {#if type === 'tag'}
                {$t('autoLabel.autoAppliedCount', { count: autoAppliedTags.length, type: $t('ai.tags').toLowerCase() })}
              {:else}
                {$t('autoLabel.autoAppliedCount', { count: autoAppliedCollections.length, type: $t('ai.collections').toLowerCase() })}
              {/if}
            </span>
          </div>
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  .ai-suggestions {
    margin-top: 8px;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    background: var(--surface-color);
    overflow: hidden;
  }

  .ai-suggestions-header {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    background: transparent;
    border: none;
    cursor: pointer;
    transition: background 0.2s;
  }

  .ai-suggestions-header:hover:not(:disabled) {
    background: var(--hover-color);
  }

  .ai-suggestions-header:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .ai-badge {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    font-weight: 500;
    color: var(--primary-on-surface);
  }

  .ai-badge svg {
    color: var(--primary-on-surface);
  }

  .suggestion-count {
    background: var(--primary-light);
    color: var(--primary-on-surface);
    padding: 2px 6px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 600;
  }

  .dropdown-icon {
    margin-left: auto;
    transition: transform 0.2s;
    color: var(--text-secondary);
  }

  .ai-suggestions-content {
    border-top: 1px solid var(--border-color);
  }

  /* Edit Modal Styles */
  .edit-modal {
    background: var(--surface-color);
    padding: 12px;
  }

  .edit-header {
    margin-bottom: 12px;
  }

  .edit-header h4 {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .edit-body {
    margin-bottom: 12px;
  }

  .edit-input {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-size: 13px;
    background: var(--surface-color);
    color: var(--text-primary);
  }

  .edit-input:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .edit-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }

  /* Match comment edit button styling */
  .btn-cancel, .btn-save {
    padding: 0.4rem 0.8rem;
    border-radius: 10px;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    border: none;
    min-width: 60px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .btn-save {
    background: var(--primary-color);
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .btn-save:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .btn-save:active {
    transform: scale(1);
  }

  .btn-save:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-cancel {
    background: var(--text-secondary);
    color: white;
    border: 1px solid var(--text-secondary);
    box-shadow: 0 2px 4px rgba(107, 114, 128, 0.2);
  }

  .btn-cancel:hover {
    background: var(--text-color);
    border: 1px solid var(--text-color);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(75, 85, 99, 0.25);
  }

  .btn-cancel:active {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(75, 85, 99, 0.2);
  }

  /* Chips Layout */
  .suggestions-chips {
    padding: 8px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .suggestion-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 6px 4px 4px;
    border-radius: 16px;
    border: 1px solid;
    transition: all 0.2s;
  }

  .chip-name {
    font-size: 12px;
    font-weight: 500;
    color: var(--text-primary);
    padding: 0 4px;
  }

  .chip-add, .chip-edit {
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: none;
    cursor: pointer;
    padding: 2px;
    border-radius: 50%;
    transition: all 0.15s;
    color: var(--text-secondary);
  }

  .chip-add {
    width: 18px;
    height: 18px;
  }

  .chip-edit {
    width: 16px;
    height: 16px;
  }

  .chip-add:hover:not(:disabled) {
    background: rgba(0, 0, 0, 0.1);
    color: var(--primary-on-surface);
  }

  .chip-edit:hover:not(:disabled) {
    background: rgba(0, 0, 0, 0.1);
    color: var(--text-primary);
  }

  .chip-add:disabled, .chip-edit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  /* Dark mode adjustments */
  :global([data-theme='dark']) .chip-add:hover:not(:disabled),
  :global([data-theme='dark']) .chip-edit:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.1);
  }

  .auto-applied-banner {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    font-size: 11px;
    color: var(--ai-accent-color, var(--primary-color, #3b82f6));
    background: rgba(59, 130, 246, 0.08);
    border-top: 1px solid var(--border-color);
  }

  .sparkle-icon {
    color: var(--ai-accent-color, var(--primary-color, #3b82f6));
    flex-shrink: 0;
  }
</style>
