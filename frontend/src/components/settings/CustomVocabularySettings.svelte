<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
  import { onMount } from 'svelte';
  import {
    CustomVocabularyApi,
    type CustomVocabularyItem,
    type CustomVocabularyCreate,
  } from '$lib/api/asrSettings';
  import { toastStore } from '$stores/toast';
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
    {#each DOMAINS as domain (domain)}
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
        {#each DOMAINS.filter(d => d !== 'all') as d (d)}
          <option value={d}>{getDomainLabel(d)}</option>
        {/each}
      </select>
      <input
        type="text"
        bind:value={newCategory}
        placeholder={$t('settings.vocabulary.categoryPlaceholder')}
        class="category-input"
      />
      <button class="btn btn-primary" on:click={addTerm} disabled={addingTerm || !newTerm.trim()}>
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
              <span class="system-badge">{$t('settings.customVocabulary.systemBadge')}</span>
            {/if}
          </div>
          <div class="term-actions">
            <span class="toggle-wrap" title={term.is_active ? $t('settings.customVocabulary.disable') : $t('settings.customVocabulary.enable')}>
            <Toggle
              checked={term.is_active}
              onchange={() => toggleActive(term)}
              disabled={term.is_system}
              label={term.is_active ? $t('settings.customVocabulary.disable') : $t('settings.customVocabulary.enable')}
            />
            </span>
            {#if !term.is_system}
              {#if showDeleteConfirm && termToDelete?.id === term.id}
                <span class="delete-confirm-inline">
                  <button class="btn btn-danger btn-sm" on:click={confirmDeleteTerm} title={$t('common.confirm')}>
                    {$t('common.confirm')}
                  </button>
                  <button class="btn btn-secondary btn-sm" on:click={cancelDelete} title={$t('common.cancel')}>
                    {$t('common.cancel')}
                  </button>
                </span>
              {:else}
                <button
                  class="btn btn-icon-only btn-icon-danger"
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
    <button class="btn btn-secondary" on:click={() => showBulkImport = !showBulkImport}>
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
            {#each DOMAINS.filter(d => d !== 'all') as d (d)}
              <option value={d}>{getDomainLabel(d)}</option>
            {/each}
          </select>
          <button class="btn btn-primary" on:click={bulkImport} disabled={importing || !bulkText.trim()}>
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
    gap: var(--space-4);
    max-width: 800px;
  }

  .info-note {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2-5) var(--space-4);
    background: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .domain-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1-5);
    padding-bottom: var(--space-1);
    border-bottom: 1px solid var(--border-color);
  }

  .domain-tab {
    padding: var(--space-1-5) var(--space-3);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    cursor: pointer;
    background: transparent;
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    transition: all var(--duration-fast);
  }

  .domain-tab:hover { color: var(--text-color); border-color: var(--border-hover); }

  .domain-tab.active {
    background: var(--primary-color);
    border-color: var(--primary-color);
    color: var(--text-on-solid);
  }

  .add-form {
    background: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    padding: var(--space-3-5);
  }

  .add-row {
    display: flex;
    gap: var(--space-2);
    flex-wrap: wrap;
  }

  .term-input {
    flex: 2;
    min-width: 140px;
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-sm);
  }

  .domain-select, .category-input {
    flex: 1;
    min-width: 110px;
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-sm);
  }

  .list-loading, .empty-terms {
    text-align: center;
    padding: var(--space-6);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .terms-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .term-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--card-background);
    transition: all var(--duration-fast) ease;
    gap: var(--space-3);
  }

  .term-row.inactive { opacity: 0.5; }

  .term-row:hover {
    background: var(--hover-color);
    border-color: var(--focus-ring-color);
  }

  .term-info {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex: 1;
    min-width: 0;
    flex-wrap: wrap;
  }

  .term-text {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .domain-badge {
    padding: var(--space-0-5) var(--space-1-5);
    background: rgba(var(--primary-color-rgb), 0.1);
    color: var(--primary-on-surface);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
  }

  .category-text {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .system-badge {
    padding: var(--space-0-5) var(--space-1-5);
    background: color-mix(in srgb, var(--text-secondary) 15%, transparent);
    color: var(--text-secondary);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
  }

  .term-actions {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-shrink: 0;
  }

  .toggle-wrap {
    display: inline-flex;
  }

  .delete-confirm-inline {
    display: flex;
    align-items: center;
    gap: var(--space-1);
  }

  .bulk-section {
    border-top: 1px solid var(--border-color);
    padding-top: var(--space-3-5);
  }

  .bulk-form {
    margin-top: var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .bulk-textarea {
    width: 100%;
    padding: var(--space-2) var(--space-3);
    font-size: var(--font-size-sm);
    resize: vertical;
    box-sizing: border-box;
    font-family: var(--font-mono);
  }

  .bulk-row {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
