<script lang="ts">
  import { onMount } from 'svelte';
  import {
    CustomVocabularyApi,
    type CustomVocabularyItem,
    type CustomVocabularyCreate,
  } from '../../lib/api/asrSettings';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

  const DOMAINS = ['all', 'medical', 'legal', 'corporate', 'government', 'technical', 'general'];

  let loading = false;
  let terms: CustomVocabularyItem[] = [];
  let selectedDomain = 'all';

  // Add term form
  let newTerm = '';
  let newDomain = 'general';
  let newCategory = '';
  let addingTerm = false;

  // Bulk import
  let showBulkImport = false;
  let bulkText = '';
  let bulkDomain = 'general';
  let importing = false;

  // Delete confirmation
  let termToDelete: CustomVocabularyItem | null = null;
  let showDeleteConfirm = false;

  onMount(async () => {
    await loadVocabulary();
  });

  async function loadVocabulary() {
    loading = true;
    try {
      terms = await CustomVocabularyApi.getVocabulary(selectedDomain);
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.vocabulary.loadFailed')), 4000);
    } finally {
      loading = false;
    }
  }

  async function selectDomain(domain: string) {
    selectedDomain = domain;
    await loadVocabulary();
  }

  async function addTerm() {
    if (!newTerm.trim()) return;
    addingTerm = true;
    try {
      const data: CustomVocabularyCreate = {
        term: newTerm.trim(),
        domain: newDomain,
      };
      if (newCategory.trim()) data.category = newCategory.trim();
      const created = await CustomVocabularyApi.createTerm(data);
      // Only append to local list if the new term's domain matches the current view.
      // When "all" is selected, terms from any domain belong in the list.
      // When a specific domain is selected, the API returned only that domain's terms,
      // so appending a term from a different domain would corrupt the filtered view.
      if (selectedDomain === 'all' || created.domain === selectedDomain) {
        terms = [...terms, created];
      }
      newTerm = '';
      newCategory = '';
      toastStore.success($t('settings.vocabulary.termAdded', { term: created.term }));
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.vocabulary.addFailed')), 4000);
    } finally {
      addingTerm = false;
    }
  }

  async function toggleActive(term: CustomVocabularyItem) {
    try {
      const updated = await CustomVocabularyApi.updateTerm(term.id, { is_active: !term.is_active });
      terms = terms.map(t => t.id === updated.id ? updated : t);
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.vocabulary.updateFailed')), 4000);
    }
  }

  function requestDeleteTerm(term: CustomVocabularyItem) {
    termToDelete = term;
    showDeleteConfirm = true;
  }

  function cancelDelete() {
    termToDelete = null;
    showDeleteConfirm = false;
  }

  async function confirmDeleteTerm() {
    if (!termToDelete) return;
    const term = termToDelete;
    termToDelete = null;
    showDeleteConfirm = false;
    try {
      await CustomVocabularyApi.deleteTerm(term.id);
      terms = terms.filter(t => t.id !== term.id);
      toastStore.success($t('settings.vocabulary.termRemoved', { term: term.term }));
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.vocabulary.deleteFailed')), 4000);
    }
  }

  async function bulkImport() {
    const lines = bulkText.split('\n').map(l => l.trim()).filter(Boolean);
    if (!lines.length) return;
    importing = true;
    try {
      const termsToImport: CustomVocabularyCreate[] = lines.map(term => ({ term, domain: bulkDomain }));
      const result = await CustomVocabularyApi.bulkImport(termsToImport);
      toastStore.success($t('settings.vocabulary.importSuccess', { created: result.created, skipped: result.skipped }));
      bulkText = '';
      showBulkImport = false;
      await loadVocabulary();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.vocabulary.importFailed')), 5000);
    } finally {
      importing = false;
    }
  }

  $: filteredTerms = selectedDomain === 'all'
    ? terms
    : terms.filter(t => t.domain === selectedDomain);

  function getDomainLabel(domain: string): string {
    const labels: Record<string, string> = {
      all: $t('settings.customVocabulary.domains.all'),
      medical: $t('settings.customVocabulary.domains.medical'),
      legal: $t('settings.customVocabulary.domains.legal'),
      corporate: $t('settings.customVocabulary.domains.corporate'),
      government: $t('settings.customVocabulary.domains.government'),
      technical: $t('settings.customVocabulary.domains.technical'),
      general: $t('settings.customVocabulary.domains.general'),
    };
    return labels[domain] || domain;
  }
</script>

<div class="vocab-settings">
  <!-- Info note -->
  <div class="info-note">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
    </svg>
    {$t('settings.customVocabulary.description')} — {$t('settings.customVocabulary.localNote')}
  </div>

  <!-- Domain filter tabs -->
  <div class="domain-tabs">
    {#each DOMAINS as domain}
      <button
        class="domain-tab"
        class:active={selectedDomain === domain}
        on:click={() => selectDomain(domain)}
      >
        {getDomainLabel(domain)}
      </button>
    {/each}
  </div>

  <!-- Add term form -->
  <div class="add-form">
    <div class="add-row">
      <input
        type="text"
        bind:value={newTerm}
        placeholder={$t('settings.customVocabulary.addTerm') + '...'}
        class="term-input"
        on:keydown={e => e.key === 'Enter' && addTerm()}
      />
      <select bind:value={newDomain} class="domain-select">
        {#each DOMAINS.filter(d => d !== 'all') as d}
          <option value={d}>{getDomainLabel(d)}</option>
        {/each}
      </select>
      <input
        type="text"
        bind:value={newCategory}
        placeholder={$t('settings.vocabulary.categoryPlaceholder')}
        class="category-input"
      />
      <button class="btn-add" on:click={addTerm} disabled={addingTerm || !newTerm.trim()}>
        {addingTerm ? '...' : $t('settings.customVocabulary.addTerm')}
      </button>
    </div>
  </div>

  <!-- Terms list -->
  {#if loading}
    <div class="list-loading">{$t('settings.vocabulary.loading')}</div>
  {:else if filteredTerms.length === 0}
    <div class="empty-terms">
      <p>{$t('settings.vocabulary.noTerms', { domain: getDomainLabel(selectedDomain) })}</p>
    </div>
  {:else}
    <div class="terms-list">
      {#each filteredTerms as term (term.id)}
        <div class="term-row" class:inactive={!term.is_active}>
          <div class="term-info">
            <span class="term-text">{term.term}</span>
            <span class="domain-badge">{term.domain}</span>
            {#if term.category}
              <span class="category-text">{term.category}</span>
            {/if}
            {#if term.is_system}
              <span class="system-badge">system</span>
            {/if}
          </div>
          <div class="term-actions">
            <label class="toggle" title={term.is_active ? $t('settings.customVocabulary.disable') : $t('settings.customVocabulary.enable')}>
              <input
                type="checkbox"
                checked={term.is_active}
                on:change={() => toggleActive(term)}
                disabled={term.is_system}
              />
              <span class="toggle-slider"></span>
            </label>
            {#if !term.is_system}
              {#if showDeleteConfirm && termToDelete?.id === term.id}
                <span class="delete-confirm-inline">
                  <button class="btn-confirm-delete" on:click={confirmDeleteTerm} title={$t('common.confirm')}>
                    {$t('common.confirm')}
                  </button>
                  <button class="btn-cancel-delete" on:click={cancelDelete} title={$t('common.cancel')}>
                    {$t('common.cancel')}
                  </button>
                </span>
              {:else}
                <button
                  class="btn-delete-term"
                  on:click={() => requestDeleteTerm(term)}
                  title={$t('settings.customVocabulary.removeTerm')}
                  aria-label={$t('settings.customVocabulary.removeTerm') + ': ' + term.term}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="3,6 5,6 21,6"/>
                    <path d="m19,6v14a2,2 0 0,1 -2,2H7a2,2 0 0,1 -2,-2V6m3,0V4a2,2 0 0,1 2,-2h4a2,2 0 0,1 2,2v2"/>
                    <line x1="10" y1="11" x2="10" y2="17"/>
                    <line x1="14" y1="11" x2="14" y2="17"/>
                  </svg>
                </button>
              {/if}
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}

  <!-- Bulk import toggle -->
  <div class="bulk-section">
    <button class="btn-bulk-toggle" on:click={() => showBulkImport = !showBulkImport}>
      {showBulkImport ? $t('settings.customVocabulary.hideBulkImport') : $t('settings.customVocabulary.bulkImport')}
    </button>

    {#if showBulkImport}
      <div class="bulk-form">
        <textarea
          bind:value={bulkText}
          placeholder={$t('settings.customVocabulary.bulkImportPlaceholder')}
          class="bulk-textarea"
          rows="6"
        ></textarea>
        <div class="bulk-row">
          <select bind:value={bulkDomain} class="domain-select">
            {#each DOMAINS.filter(d => d !== 'all') as d}
              <option value={d}>{getDomainLabel(d)}</option>
            {/each}
          </select>
          <button class="btn-import" on:click={bulkImport} disabled={importing || !bulkText.trim()}>
            {importing ? $t('settings.vocabulary.importing') : $t('settings.vocabulary.import')}
          </button>
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  .vocab-settings {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    max-width: 800px;
    margin: 0 auto;
  }

  .info-note {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.625rem 1rem;
    background: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .domain-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    padding-bottom: 0.25rem;
    border-bottom: 1px solid var(--border-color);
  }

  .domain-tab {
    padding: 0.35rem 0.75rem;
    border-radius: 6px;
    font-size: 0.8125rem;
    cursor: pointer;
    background: transparent;
    border: 1px solid var(--border-color);
    color: var(--text-muted);
    transition: all 0.15s;
  }

  .domain-tab:hover { color: var(--text-color); border-color: var(--border-hover, #6b7280); }

  .domain-tab.active {
    background: #3b82f6;
    border-color: var(--primary-color);
    color: white;
  }

  .add-form {
    background: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.875rem;
  }

  .add-row {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .term-input {
    flex: 2;
    min-width: 140px;
    padding: 0.45rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 5px;
    background: var(--bg-primary, transparent);
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  .domain-select, .category-input {
    flex: 1;
    min-width: 110px;
    padding: 0.45rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 5px;
    background: var(--bg-primary, transparent);
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  .btn-add {
    padding: 0.45rem 1rem;
    background: #3b82f6;
    color: white;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    white-space: nowrap;
    transition: all 0.15s;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-add:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-add:active:not(:disabled) {
    transform: scale(1);
  }

  .list-loading, .empty-terms {
    text-align: center;
    padding: 1.5rem;
    color: var(--text-muted);
    font-size: 0.8125rem;
  }

  .terms-list {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .term-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--card-background);
    transition: all 0.15s ease;
    gap: 0.75rem;
  }

  .term-row.inactive { opacity: 0.5; }

  .term-row:hover {
    background: var(--hover-color);
    border-color: var(--input-focus-border);
  }

  .term-info {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex: 1;
    min-width: 0;
    flex-wrap: wrap;
  }

  .term-text {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .domain-badge {
    padding: 0.1rem 0.4rem;
    background: rgba(var(--primary-color-rgb), 0.1);
    color: var(--primary-color);
    border-radius: 4px;
    font-size: 0.7rem;
  }

  .category-text {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .system-badge {
    padding: 0.1rem 0.4rem;
    background: rgba(107, 114, 128, 0.15);
    color: var(--text-muted);
    border-radius: 4px;
    font-size: 0.7rem;
  }

  .term-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  /* Toggle switch */
  .toggle {
    position: relative;
    display: inline-flex;
    align-items: center;
    cursor: pointer;
  }

  .toggle input {
    opacity: 0;
    width: 0;
    height: 0;
    position: absolute;
  }

  .toggle-slider {
    display: inline-block;
    width: 30px;
    height: 16px;
    background: var(--border-color);
    border-radius: 8px;
    transition: background 0.2s;
    position: relative;
  }

  .toggle-slider::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
    background: white;
    border-radius: 50%;
    transition: transform 0.2s;
  }

  .toggle input:checked + .toggle-slider {
    background: var(--success-color, #10b981);
  }

  .toggle input:checked + .toggle-slider::after {
    transform: translateX(14px);
  }

  .toggle input:disabled + .toggle-slider {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-delete-term {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    background: transparent;
    border: 1px solid var(--error-color, #ef4444);
    color: var(--error-color, #ef4444);
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.15s;
    flex-shrink: 0;
  }

  .btn-delete-term:hover {
    background: var(--error-color, #ef4444);
    color: white;
  }

  .delete-confirm-inline {
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }

  .btn-confirm-delete {
    padding: 0.25rem 0.625rem;
    background: var(--error-color, #ef4444);
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.75rem;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.15s;
  }

  .btn-confirm-delete:hover {
    background: var(--error-color, #ef4444);
    filter: brightness(0.85);
  }

  .btn-cancel-delete {
    padding: 0.25rem 0.625rem;
    background: transparent;
    color: var(--text-secondary, #999);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-size: 0.75rem;
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.15s;
  }

  .btn-cancel-delete:hover {
    color: var(--text-color);
    background: var(--button-hover);
  }

  .bulk-section {
    border-top: 1px solid var(--border-color);
    padding-top: 0.875rem;
  }

  .btn-bulk-toggle {
    padding: 0.4rem 0.875rem;
    background: transparent;
    border: 1px solid var(--border-color);
    color: var(--text-muted);
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.8rem;
    transition: all 0.15s;
  }

  .btn-bulk-toggle:hover { color: var(--text-color); border-color: var(--border-hover, #6b7280); }

  .bulk-form {
    margin-top: 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .bulk-textarea {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-primary, transparent);
    color: var(--text-color);
    font-size: 0.8125rem;
    resize: vertical;
    box-sizing: border-box;
    font-family: var(--font-mono, monospace);
  }

  .bulk-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .btn-import {
    padding: 0.45rem 1rem;
    background: #3b82f6;
    color: white;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.8125rem;
    white-space: nowrap;
    transition: all 0.15s;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-import:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-import:active:not(:disabled) {
    transform: scale(1);
  }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  input:focus, select:focus, textarea:focus {
    outline: none;
    border-color: var(--input-focus-border, var(--primary-color));
    box-shadow: 0 0 0 2px rgba(var(--primary-color-rgb), 0.2);
  }
</style>
