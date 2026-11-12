<script lang="ts">
  import '$components/ui/tag-pill.css';
  import FormField from '$components/ui/FormField.svelte';
  import Toggle from '$components/ui/Toggle.svelte';
  import ActivateIcon from '$components/ui/ActivateIcon.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { onMount } from 'svelte';
  import { PromptsApi, type SummaryPrompt } from '$lib/api/prompts';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import Spinner from '$components/ui/Spinner.svelte';
  import TagInput from '$components/ui/TagInput.svelte';
  import PromptCopyButton from './PromptCopyButton.svelte';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    activeAfterDelete,
    defaultPromptId,
    findPromptById,
    splitLoadedPrompts,
    userPromptsOf,
    visibleSystemPrompts
  } from '$lib/settings/promptList';
  import {
    blankPromptForm,
    cloneForm,
    isPromptFormDirty,
    isPromptFormValid,
    promptFormFrom,
    type PromptFormData
  } from '$lib/settings/promptForm';
  import { withShareState } from '$lib/settings/shareState';
  import EmptyState from '$components/ui/EmptyState.svelte';

  export let onSettingsChange: (() => void) | null = null;

  // State variables
  let loading = false;
  let saving = false;

  let allPrompts: SummaryPrompt[] = [];
  let activePrompt: SummaryPrompt | null = null;
  let selectedPromptId: string | null = null;  // UUID

  // Create/Edit prompt form
  let showCreateForm = false;
  let editingPrompt: SummaryPrompt | null = null;

  // View prompt modal
  let showViewModal = false;
  let viewingPrompt: SummaryPrompt | null = null;

  // Shared prompts
  let sharedPrompts: SummaryPrompt[] = [];

  // Delete and unsaved-changes confirmations
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    cancelText: confirmCancelLabel,
    variant: confirmVariant,
  } = confirmDialog;

  // Form dirty state tracking
  let originalFormData: PromptFormData = blankPromptForm();
  let isDirty = false;

  let formData: PromptFormData = blankPromptForm();

  $: contentTypes = [
    { value: 'general', label: $t('prompts.contentTypeGeneral') },
    { value: 'meeting', label: $t('prompts.contentTypeMeeting') },
    { value: 'interview', label: $t('prompts.contentTypeInterview') },
    { value: 'podcast', label: $t('prompts.contentTypePodcast') },
    { value: 'documentary', label: $t('prompts.contentTypeDocumentary') },
    { value: 'qa_panel', label: $t('prompts.contentTypeQaPanel') },
    { value: 'speaker_identification', label: $t('prompts.contentTypeSpeakerIdentification') }
  ];

  onMount(async () => {
    await loadData();
  });

  async function loadData() {
    loading = true;

    try {
      // Load prompts and active prompt in parallel
      const [promptsResponse, activeResponse] = await Promise.all([
        PromptsApi.getPrompts({ include_system: true, include_user: true, limit: 100 }),
        PromptsApi.getActivePrompt(),
      ]);

      // Separate own prompts from shared prompts (shared are non-owner, non-system)
      const split = splitLoadedPrompts(promptsResponse.prompts || []);
      allPrompts = split.own;
      sharedPrompts = split.shared;

      activePrompt = activeResponse.active_prompt;
      selectedPromptId = activeResponse.active_prompt_id;

      // Auto-select universal prompt if no prompt is selected
      if (!selectedPromptId && allPrompts.length > 0) {
        const fallbackId = defaultPromptId(allPrompts);
        if (fallbackId) await setActivePrompt(fallbackId);
      }
    } catch (err: unknown) {
      console.error('Error loading prompts:', err);
      toastStore.error(getErrorMessage(err, $t('prompts.loadFailed')));
    } finally {
      loading = false;
    }
  }

  async function setActivePrompt(promptId: string) {
    saving = true;

    try {
      await PromptsApi.setActivePrompt({
        prompt_id: promptId
      });

      selectedPromptId = promptId;
      activePrompt = findPromptById(promptId, allPrompts, sharedPrompts);
      toastStore.success($t('prompts.activePromptUpdated'));

      if (onSettingsChange) {
        onSettingsChange();
      }
    } catch (err: unknown) {
      console.error('Error setting active prompt:', err);
      const errorMessage = getErrorMessage(err, $t('prompts.setActiveFailed'));
      toastStore.error(errorMessage);
    } finally {
      saving = false;
    }
  }

  async function handleClone(prompt: SummaryPrompt) {
    saving = true;

    try {
      await PromptsApi.clonePrompt(prompt.uuid);
      await loadData();
      toastStore.success($t('prompts.cloneSuccess'));

      if (onSettingsChange) {
        onSettingsChange();
      }
    } catch (err: unknown) {
      console.error('Error cloning prompt:', err);
      const status = (err as { response?: { status?: number } })?.response?.status;
      const fallback =
        status === 400 ? $t('prompts.cloneLimitReached') : $t('prompts.cloneFailed');
      toastStore.error(getErrorMessage(err, fallback));
    } finally {
      saving = false;
    }
  }

  function openCreateForm() {
    showCreateForm = true;
    editingPrompt = null;
    formData = blankPromptForm();
    originalFormData = cloneForm(formData);
  }

  function openEditForm(prompt: SummaryPrompt) {
    if (prompt.is_system_default) {
      toastStore.error($t('prompts.editSystemError'));
      return;
    }

    showCreateForm = true;
    editingPrompt = prompt;
    formData = promptFormFrom(prompt);
    originalFormData = cloneForm(formData);
  }

  function closeForm(force: boolean = false) {
    if (!force && isDirty) {
      confirmDialog.request(
        $t('prompts.unsavedChanges'),
        $t('prompts.unsavedChangesMessage'),
        executeCloseForm,
        {
          confirmText: $t('prompts.discardChanges'),
          cancelText: $t('prompts.keepEditing'),
          variant: 'warning',
        }
      );
      return;
    }

    executeCloseForm();
  }

  function executeCloseForm() {
    showCreateForm = false;
    editingPrompt = null;
    formData = blankPromptForm();
    originalFormData = cloneForm(formData);
  }

  async function savePrompt() {
    if (!formData.name.trim() || !formData.prompt_text.trim()) {
      toastStore.error($t('prompts.nameAndTextRequired'));
      return;
    }

    saving = true;

    try {
      if (editingPrompt) {
        // Update existing prompt
        const updatedPrompt = await PromptsApi.updatePrompt(editingPrompt.uuid, formData);

        // Update in the list
        const index = allPrompts.findIndex(p => p.uuid === updatedPrompt.uuid);
        if (index >= 0) {
          allPrompts[index] = updatedPrompt;
          allPrompts = [...allPrompts]; // Trigger reactivity
        }

        toastStore.success($t('prompts.promptUpdated'));
      } else {
        // Create new prompt
        const newPrompt = await PromptsApi.createPrompt(formData);

        allPrompts = [...allPrompts, newPrompt];
        toastStore.success($t('prompts.promptCreated'));
      }

      // Force close after successful save (no dirty check needed)
      closeForm(true);

      if (onSettingsChange) {
        onSettingsChange();
      }
    } catch (err: unknown) {
      console.error('Error saving prompt:', err);
      toastStore.error(getErrorMessage(err, $t('prompts.saveFailed')));
    } finally {
      saving = false;
    }
  }

  function confirmDeletePrompt(prompt: SummaryPrompt) {
    if (prompt.is_system_default) {
      toastStore.error($t('prompts.deleteSystemError'));
      return;
    }

    confirmDialog.request(
      $t('prompts.deleteConfirmTitle'),
      $t('prompts.deleteConfirmMessage', { name: prompt.name }),
      () => deletePrompt(prompt),
      {
        confirmText: $t('prompts.delete'),
        cancelText: $t('prompts.cancel'),
        variant: 'danger',
      }
    );
  }

  async function deletePrompt(prompt: SummaryPrompt) {
    saving = true;

    try {
      await PromptsApi.deletePrompt(prompt.uuid);

      // Remove from list
      allPrompts = allPrompts.filter(p => p.uuid !== prompt.uuid);

      // Check if we deleted the active prompt
      const wasActivePrompt = selectedPromptId === prompt.uuid;

      // With no user prompt left the server falls back to the built-in default (Universal Content
      // Analyzer), whichever prompt had been active, so ask it which one that is.
      const afterDelete = activeAfterDelete(allPrompts, wasActivePrompt);
      if (afterDelete === 'refetch') {
        try {
          // Load fresh active prompt data - backend will automatically fall back to Universal Content Analyzer
          const activeResponse = await PromptsApi.getActivePrompt();
          selectedPromptId = activeResponse.active_prompt_id;
          activePrompt = activeResponse.active_prompt;

          if (activePrompt && activePrompt.name) {
            toastStore.success($t('prompts.promptDeletedWithFallback', { name: activePrompt.name }));
          } else {
            toastStore.success($t('prompts.promptDeletedDefaultActive'));
          }
        } catch (activeErr: unknown) {
          console.error('Error getting fallback active prompt:', activeErr);
          selectedPromptId = null;
          activePrompt = null;
          toastStore.success($t('prompts.promptDeleted'));
        }
      } else if (afterDelete === 'clear') {
        // Just clear the selection if it was active but we still have user prompts
        selectedPromptId = null;
        activePrompt = null;
        toastStore.success($t('prompts.promptDeleted'));
      } else {
        toastStore.success($t('prompts.promptDeleted'));
      }

      if (onSettingsChange) {
        onSettingsChange();
      }
    } catch (err: unknown) {
      console.error('Error deleting prompt:', err);
      toastStore.error(getErrorMessage(err, $t('prompts.deleteFailed')));
    } finally {
      saving = false;
    }
  }

  async function handleShareToggle(prompt: SummaryPrompt) {
    if (prompt.is_system_default) return;
    const newShared = !prompt.is_shared;
    const prevSharedAt = prompt.shared_at;

    // Optimistic update — flip toggle immediately for responsive UI
    allPrompts = withShareState(
      allPrompts,
      prompt.uuid,
      newShared,
      newShared ? new Date().toISOString() : undefined
    );

    saving = true;
    try {
      await PromptsApi.sharePrompt(prompt.uuid, newShared);
      toastStore.success(
        newShared ? $t('prompts.shareEnabled') : $t('prompts.shareDisabled')
      );
    } catch (err: unknown) {
      // Rollback on failure
      allPrompts = withShareState(allPrompts, prompt.uuid, !newShared, prevSharedAt);
      console.error('Error toggling share:', err);
      toastStore.error(getErrorMessage(err, $t('prompts.shareFailed')));
    } finally {
      saving = false;
    }
  }

  // Separate prompts by type - exclude speaker_identification from UI
  $: systemPrompts = visibleSystemPrompts(allPrompts);
  $: userPrompts = userPromptsOf(allPrompts);

  function viewPrompt(prompt: SummaryPrompt) {
    viewingPrompt = prompt;
    showViewModal = true;
  }

  function closeViewModal() {
    showViewModal = false;
    viewingPrompt = null;
  }

  $: isFormValid = isPromptFormValid(formData);

  // Track form changes for dirty state (tags is an array, so JSON compare works)
  $: isDirty = isPromptFormDirty(formData, originalFormData);

  // Function to prevent native tooltip flicker and position tooltip
  function removeTitle(event: MouseEvent) {
    const target = event.target as Element | null;
    // Remove title attribute to prevent native tooltip
    if (target?.hasAttribute('title')) {
      target.removeAttribute('title');
    }
    // Check parent elements too
    const element = target?.closest('[title]');
    if (element) {
      element.removeAttribute('title');
    }

    // Position the tooltip dynamically
    const tooltip = target?.closest('.info-tooltip');
    if (!(tooltip instanceof HTMLElement)) return;
    const rect = tooltip.getBoundingClientRect();

    tooltip.style.setProperty('--tooltip-left', `${rect.left + rect.width / 2}px`);
    tooltip.style.setProperty('--tooltip-top', `${rect.bottom}px`);
  }
</script>

{#snippet activateControl(prompt: SummaryPrompt)}
    {#if selectedPromptId === prompt.uuid}
      <div class="config-status currently-active">
        <div class="status-indicator">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="20,6 9,17 4,12"/>
          </svg>
        </div>
        <span class="status-text">{$t('prompts.currentlyActive')}</span>
      </div>
    {:else}
      <button
        class="btn btn-success activate-button"
        on:click={() => setActivePrompt(prompt.uuid)}
        disabled={saving}
        title={$t('prompts.makeActive')}
      >
        <ActivateIcon strokeLinecap="round" />
        {$t('prompts.activate')}
      </button>
    {/if}
{/snippet}

{#snippet viewButton(prompt: SummaryPrompt)}
    <button
      class="btn btn-secondary view-button"
      on:click={() => viewPrompt(prompt)}
      title={$t('prompts.viewPrompt')}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
        <circle cx="12" cy="12" r="3"/>
      </svg>
    </button>
{/snippet}

<div class="prompt-settings provider-panel">
  {#if loading}
    <SettingsSkeleton variant="form" count={3} label={$t('prompts.loading')} />
  {:else}

    <!-- System Prompts -->
    {#if systemPrompts.length > 0}
      <div class="saved-configs-section">
        <div class="section-header">
          <h4>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>
            </svg>
            {$t('prompts.systemPrompts')}
          </h4>
        </div>

        <div class="config-list">
          {#each systemPrompts as prompt (prompt.uuid)}
            <div class="config-item" class:active={selectedPromptId === prompt.uuid}>
              <div class="config-info">
                <div class="config-name">
                  {prompt.name}
                  <span
                    class="info-tooltip"
                    role="tooltip"
                    data-tooltip={$t('prompts.systemPromptTooltip')}
                    on:mouseenter={removeTitle}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"/>
                      <path d="M12 16v-4"/>
                      <path d="M12 8h.01"/>
                    </svg>
                  </span>
                </div>
                <div class="config-provider">{prompt.content_type || 'General'}</div>
                {#if prompt.description}
                  <div class="config-url">{prompt.description}</div>
                {/if}
                {#if prompt.linked_collections && prompt.linked_collections.length > 0}
                  <div class="linked-collections">
                    <span class="linked-label">{$t('prompts.usedByCollections')}:</span>
                    {#each prompt.linked_collections as col (col.uuid)}
                      <span class="collection-tag">{col.name}</span>
                    {/each}
                  </div>
                {/if}
              </div>
              <div class="prompt-actions">
                {@render activateControl(prompt)}

                {@render viewButton(prompt)}
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    <!-- Custom Prompts -->
    <div class="saved-configs-section">
      <div class="section-header">
        <h4>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14,2 14,8 20,8"/>
            <line x1="16" y1="13" x2="8" y2="13"/>
            <line x1="16" y1="17" x2="8" y2="17"/>
            <polyline points="10,9 9,9 8,9"/>
          </svg>
          {$t('prompts.customPrompts')}
        </h4>
        {#if userPrompts.length > 0 || sharedPrompts.length > 0}
          <button class="btn btn-primary create-config-button" on:click={openCreateForm} title={$t('prompts.createNew')}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19"/>
              <line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            {$t('prompts.createPrompt')}
          </button>
        {/if}
      </div>

      {#if userPrompts.length > 0}
        <div class="config-list">
          {#each userPrompts as prompt (prompt.uuid)}
            <div class="config-item" class:active={selectedPromptId === prompt.uuid}>
              <div class="config-info">
                <div class="config-name">
                  {prompt.name}
                  {#if prompt.is_shared}<span class="share-badge">{$t('prompts.shared')}</span>{/if}
                </div>
                <div class="config-provider">{prompt.content_type || 'General'}</div>
                {#if prompt.description}
                  <div class="config-url">{prompt.description}</div>
                {/if}
                {#if prompt.tags && prompt.tags.length > 0}
                  <div class="tag-pills">
                    {#each prompt.tags as tag, i (i)}
                      <span class="tag-pill">{tag}</span>
                    {/each}
                  </div>
                {/if}
                {#if prompt.linked_collections && prompt.linked_collections.length > 0}
                  <div class="linked-collections">
                    <span class="linked-label">{$t('prompts.usedByCollections')}:</span>
                    {#each prompt.linked_collections as col (col.uuid)}
                      <span class="collection-tag">{col.name}</span>
                    {/each}
                  </div>
                {/if}
                <div class="provider-share-row">
                  <div class="toggle-label">
                    <Toggle id="share-prompt-{prompt.uuid}" checked={!!prompt.is_shared} onchange={() => handleShareToggle(prompt)} disabled={saving} />
                    <label class="toggle-text" for="share-prompt-{prompt.uuid}">{$t('prompts.shareGlobally')}</label>
                  </div>
                </div>
              </div>
              <div class="prompt-actions">
                {@render activateControl(prompt)}

                {@render viewButton(prompt)}
                <button
                  class="btn btn-secondary edit-button"
                  on:click={() => openEditForm(prompt)}
                  disabled={saving}
                  title={$t('prompts.editPrompt')}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                    <path d="m18.5 2.5 a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                  </svg>
                </button>
                <button
                  class="btn btn-danger-outline delete-config-button"
                  on:click={() => confirmDeletePrompt(prompt)}
                  disabled={saving}
                  title={`Delete prompt: ${prompt.name}`}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="3,6 5,6 21,6"/>
                    <path d="m19,6v14a2,2 0 0,1 -2,2H7a2,2 0 0,1 -2,-2V6m3,0V4a2,2 0 0,1 2,-2h4a2,2 0 0,1 2,2v2"/>
                    <line x1="10" y1="11" x2="10" y2="17"/>
                    <line x1="14" y1="11" x2="14" y2="17"/>
                  </svg>
                </button>
              </div>
            </div>
          {/each}
        </div>
      {:else}
        <EmptyState bordered titleSize="xl" padding="var(--space-12) var(--space-8)" title={$t('prompts.noCustomPrompts')} description={$t('prompts.noCustomPromptsDesc')}>
          <svg slot="icon" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14,2 14,8 20,8"/>
            <line x1="16" y1="13" x2="8" y2="13"/>
            <line x1="16" y1="17" x2="8" y2="17"/>
            <polyline points="10,9 9,9 8,9"/>
          </svg>
          <button class="btn btn-primary" on:click={openCreateForm}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19"/>
              <line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            {$t('prompts.createFirstPrompt')}
          </button>
        </EmptyState>
      {/if}
    </div>

    <!-- Shared Prompts from Others -->
    {#if sharedPrompts.length > 0}
      <div class="saved-configs-section">
        <div class="section-header shared-section-header">
          <h4>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <line x1="2" y1="12" x2="22" y2="12"/>
              <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
            </svg>
            {$t('prompts.sharedByOthers')}
          </h4>
        </div>

        <div class="config-list">
          {#each sharedPrompts as prompt (prompt.uuid)}
            <div class="config-item shared" class:active={selectedPromptId === prompt.uuid}>
              <div class="config-info">
                <div class="config-name">
                  {prompt.name}
                  <span class="share-badge">{$t('prompts.shared')}</span>
                  {#if prompt.author_role === 'admin' || prompt.author_role === 'super_admin'}
                    <span class="admin-badge">{$t('settings.sharing.adminBadge')}</span>
                  {/if}
                </div>
                <div class="config-provider">{prompt.content_type || 'General'}</div>
                {#if prompt.description}
                  <div class="config-url">{prompt.description}</div>
                {/if}
                {#if prompt.tags && prompt.tags.length > 0}
                  <div class="tag-pills">
                    {#each prompt.tags as tag, i (i)}
                      <span class="tag-pill">{tag}</span>
                    {/each}
                  </div>
                {/if}
                {#if prompt.author_name}
                  <div class="shared-by">{$t('prompts.sharedBy', { name: prompt.author_name })}</div>
                {/if}
                {#if prompt.shared_by_name && prompt.shared_by_name !== prompt.author_name}
                  <div class="shared-by">{$t('prompts.sharedByAttribution', { admin: prompt.shared_by_name })}</div>
                {/if}
              </div>
              <div class="prompt-actions">
                {@render activateControl(prompt)}

                <button
                  class="btn btn-secondary clone-button"
                  on:click={() => handleClone(prompt)}
                  disabled={saving}
                  title={$t('prompts.clone')}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                  </svg>
                  {$t('prompts.clone')}
                </button>

                {@render viewButton(prompt)}
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/if}
  {/if}

  <!-- Create/Edit Form Modal -->
  <BaseModal
    isOpen={showCreateForm}
    onClose={() => closeForm()}
    size="md"
  >
    <svelte:fragment slot="header">
      <h2 class="modal-title">{editingPrompt ? $t('prompts.editExisting') : $t('prompts.createNew')}</h2>
      {#if isDirty}
        <span class="unsaved-dot" role="img" aria-label={$t('prompts.unsavedChanges')} title={$t('prompts.unsavedChanges')}><MenuIcon name="dot" size={10} /></span>
      {/if}
    </svelte:fragment>
    <form id="prompt-form" on:submit|preventDefault={savePrompt} class="prompt-form">
      <FormField id="name" label={$t('prompts.promptName')}>
        <input
          type="text"
          id="name"
          bind:value={formData.name}
          disabled={saving}
          class="form-control"
          placeholder={$t('prompts.promptNamePlaceholder')}
          required
        />
      </FormField>

      <FormField id="content_type" label={$t('prompts.contentType')}>
        <select
          id="content_type"
          bind:value={formData.content_type}
          disabled={saving}
          class="form-control"
        >
          {#each contentTypes as type (type.value)}
            <option value={type.value}>{type.label}</option>
          {/each}
        </select>
      </FormField>

      <FormField id="description" label={$t('prompts.descriptionLabel')}>
        <input
          type="text"
          id="description"
          bind:value={formData.description}
          disabled={saving}
          class="form-control"
          placeholder={$t('prompts.descriptionPlaceholder')}
        />
      </FormField>

      <FormField id="tags" label={$t('prompts.tags')} hint={$t('prompts.tagsHelp')} let:describedby>
        <TagInput id="tags" {describedby} bind:tags={formData.tags} placeholder={$t('prompts.tagsPlaceholder')} disabled={saving} />
      </FormField>

      <!-- Share with all users -->
      <div class="provider-share-row">
        <div class="toggle-label">
          <Toggle id="promptsettings-toggle-1" checked={formData.is_shared} onchange={(v) => (formData.is_shared = v)} disabled={saving} />
          <label class="toggle-text" for="promptsettings-toggle-1">{$t('prompts.shareGlobally')}</label>
        </div>
      </div>

      <FormField id="prompt_text" label={$t('prompts.promptText')} labelSize="sm" hint={$t('prompts.promptTextHelp')} let:describedby>
        <svelte:fragment slot="label-after">
          {#if formData.prompt_text.trim()}
            <PromptCopyButton text={formData.prompt_text} />
          {/if}
        </svelte:fragment>
        <textarea
          id="prompt_text"
          aria-describedby={describedby}
          bind:value={formData.prompt_text}
          disabled={saving}
          class="form-control textarea"
          rows="8"
          placeholder={$t('prompts.promptTextPlaceholder')}
          required
        ></textarea>
        <div class="llm-hint">
          <strong>
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="tip-icon">
              <circle cx="12" cy="12" r="5"></circle>
              <line x1="12" y1="1" x2="12" y2="3"></line>
              <line x1="12" y1="21" x2="12" y2="23"></line>
              <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line>
              <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line>
              <line x1="1" y1="12" x2="3" y2="12"></line>
              <line x1="21" y1="12" x2="23" y2="12"></line>
              <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line>
              <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>
            </svg>
            {$t('prompts.llmTip')}
          </strong> {$t('prompts.llmTipText')}
        </div>
      </FormField>
    </form>
    <svelte:fragment slot="footer">
      <button type="button" class="btn btn-secondary btn-lg"
              on:click={() => closeForm()} disabled={saving}>
        {$t('prompts.cancel')}
      </button>
      <button type="submit" form="prompt-form"
              class="btn btn-primary btn-lg"
              disabled={saving || !isFormValid}>
        {#if saving}<Spinner size="small" color="white" />{/if}
        {editingPrompt ? $t('prompts.updatePrompt') : $t('prompts.createPromptBtn')}
      </button>
    </svelte:fragment>
  </BaseModal>

  <!-- View Prompt Modal -->
  <BaseModal
    isOpen={showViewModal && !!viewingPrompt}
    title={viewingPrompt?.name ?? ''}
    onClose={closeViewModal}
    size="lg"
  >
    {#if viewingPrompt}
      <div class="prompt-details">
        <div class="detail-row">
          <strong>{$t('prompts.type')}</strong> {viewingPrompt.content_type || $t('prompts.contentTypeGeneral')}
        </div>
        {#if viewingPrompt.description}
          <div class="detail-row">
            <strong>{$t('prompts.descriptionLabel')}:</strong> {viewingPrompt.description}
          </div>
        {/if}
        <div class="detail-row">
          <strong>{$t('prompts.systemPrompt')}</strong> {viewingPrompt.is_system_default ? $t('common.yes') : $t('common.no')}
        </div>
        {#if viewingPrompt.linked_collections && viewingPrompt.linked_collections.length > 0}
          <div class="detail-row">
            <strong>{$t('prompts.usedByCollections')}:</strong>
            <div class="linked-collections-modal">
              {#each viewingPrompt.linked_collections as col (col.uuid)}
                <span class="collection-tag">{col.name}</span>
              {/each}
            </div>
          </div>
        {/if}
      </div>
      <div class="prompt-text-container">
        <div class="prompt-text-header">
          <strong>{$t('prompts.promptTextLabel')}:</strong>
          <PromptCopyButton text={viewingPrompt?.prompt_text ?? ''} />
        </div>
        <div class="prompt-text-display">{viewingPrompt.prompt_text}</div>
      </div>
    {/if}
  </BaseModal>

  <!-- Delete and unsaved-changes confirmations -->
  <ConfirmationModal
    bind:isOpen={$confirmOpen}
    title={$confirmTitle}
    message={$confirmMessage}
    confirmText={$confirmLabel}
    cancelText={$confirmCancelLabel}
    variant={$confirmVariant}
    on:confirm={confirmDialog.confirm}
    on:cancel={confirmDialog.cancel}
    on:close={confirmDialog.cancel}
  />
</div>

<style>
  .prompt-settings {
    max-width: 800px;
  }



  .prompt-actions {
    display: flex;
    gap: var(--space-2);
    flex-wrap: wrap;
    align-items: center;
  }

  /* New button styles to match LLM provider page */

  .config-status {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-1-5) var(--space-3);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    width: fit-content;
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--space-4);
    padding-bottom: var(--space-2);
    border-bottom: 1px solid var(--border-color);
  }

  .section-header h4 {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    font-size: 1.125rem;
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  /* Additional styles to match LLM provider structure */
  .config-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .config-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--card-bg);
    transition: all var(--duration-base) ease;
  }

  .config-name {
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    font-size: var(--font-size-sm);
    margin-bottom: var(--space-1);
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .info-tooltip {
    display: inline-flex;
    align-items: center;
    color: var(--text-secondary);
    opacity: 0.6;
    cursor: help;
    transition: opacity var(--duration-base) ease;
    position: relative;
  }

  .info-tooltip:hover {
    opacity: 1;
    color: var(--primary-on-surface);
  }

  .info-tooltip[data-tooltip]:hover::after {
    content: attr(data-tooltip);
    position: fixed;
    left: var(--tooltip-left, 50%);
    top: var(--tooltip-top, 50%);
    background: var(--tooltip-bg);
    color: var(--tooltip-text);
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    font-weight: normal;
    max-width: 320px;
    white-space: normal;
    z-index: var(--z-toast);
    line-height: 1.4;
    box-shadow: var(--shadow-lg);
    text-align: start;
    pointer-events: none;
    transform: translate(-50%, 8px);
  }

  .config-provider {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    margin-bottom: var(--space-1);
  }

  .config-url {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    opacity: 0.7;
    max-width: 400px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    text-overflow: ellipsis;
    line-height: 1.4;
  }

  .linked-collections {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1-5);
    margin-top: var(--space-1-5);
    font-size: var(--font-size-xs);
  }

  .linked-label {
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    white-space: nowrap;
  }

  .collection-tag {
    display: inline-flex;
    align-items: center;
    padding: var(--space-0-5) var(--space-2);
    background: rgba(var(--accent-rgb), 0.1);
    color: var(--primary-on-surface);
    border-radius: var(--radius-xl);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    white-space: nowrap;
  }

  .linked-collections-modal {
    display: inline-flex;
    flex-wrap: wrap;
    gap: var(--space-1-5);
    margin-top: var(--space-1);
  }

  :global([data-theme='dark']) .collection-tag {
    background: rgba(var(--primary-color-rgb), 0.2);
  }

  .prompt-text-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-3);
  }

  .prompt-form {
    padding: var(--space-6);
    display: flex;
    flex-direction: column;
    gap: var(--space-5);
  }

  label {
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .form-control {
    padding: var(--space-3);
    font-size: var(--font-size-sm);
  }

  .form-control.textarea {
    resize: vertical;
    font-family: Monaco, Menlo, 'Ubuntu Mono', monospace;
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .form-control:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  @media (max-width: 768px) {
    .section-header {
      flex-direction: column;
      align-items: stretch;
      gap: var(--space-4);
    }

    .prompt-actions {
      justify-content: flex-start;
    }

    .prompt-actions button {
      min-height: 44px;
    }

  }

  /* LLM Hint Styling */
  .llm-hint {
    margin-top: var(--space-3);
    padding: var(--space-3);
    background: var(--color-info-bg);
    border: 1px solid var(--color-info-border);
    color: var(--text-color);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    line-height: 1.3;
  }

  .llm-hint strong {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-1);
    color: var(--info-text);
  }

  .tip-icon {
    flex-shrink: 0;
  }

  /* View Modal Styling */
  .prompt-details {
    margin-bottom: var(--space-6);
  }

  .detail-row {
    margin-bottom: var(--space-3);
    font-size: var(--font-size-sm);
  }

  .detail-row strong {
    color: var(--primary-on-surface);
    margin-inline-end: var(--space-2);
  }

  .prompt-text-container {
    margin-top: var(--space-6);
  }

  .prompt-text-container strong {
    display: block;
    margin-bottom: var(--space-3);
    color: var(--primary-on-surface);
  }

  .prompt-text-display {
    font-family: Monaco, Menlo, 'Ubuntu Mono', monospace;
    font-size: var(--font-size-sm);
    line-height: 1.6;
    color: var(--text-color);
    white-space: pre-wrap;
    overflow-wrap: break-word;
    background-color: var(--surface-color);
    padding: var(--space-4);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-color);
    max-height: 400px;
    overflow-y: auto;
  }

  /* Shared config styles */
  .config-item.shared {
    border-inline-start: 3px solid var(--info-color);
    background: rgba(var(--accent-rgb), 0.04);
  }

  :global([data-theme='dark']) .config-item.shared {
    background: rgba(var(--info-color-rgb), 0.06);
  }

  .share-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-xl);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    background: rgba(var(--accent-rgb), 0.12);
    color: var(--primary-on-surface);
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }

  :global([data-theme='dark']) .share-badge {
    background: rgba(var(--info-color-rgb), 0.15);
  }

  .admin-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-xl);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    background: rgba(var(--warning-color-rgb), 0.12);
    color: var(--warning-text);
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }

  :global([data-theme='dark']) .admin-badge {
    background: rgba(var(--warning-color-rgb), 0.2);
  }

  .shared-by {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-top: var(--space-1-5);
    font-style: italic;
  }

  .shared-section-header h4 {
    color: var(--primary-on-surface);
  }

  /* Tag pills */
  .tag-pills {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    margin-top: var(--space-1-5);
  }

  :global([data-theme='dark']) .tag-pill {
    background: rgba(var(--primary-color-rgb), 0.2);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    font-weight: var(--font-weight-regular);
  }

  .toggle-text {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .modal-title {
    margin: 0;
    font-size: var(--font-size-xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .unsaved-dot {
    display: inline-flex;
    align-items: center;
    color: var(--warning-text);
    flex-shrink: 0;
  }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
