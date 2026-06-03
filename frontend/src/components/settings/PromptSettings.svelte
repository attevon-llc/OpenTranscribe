<script lang="ts">
  import { onMount } from 'svelte';
  import { PromptsApi, type SummaryPrompt, type ActivePromptResponse, type SummaryPromptCreate, type SummaryPromptUpdate, type SharedPromptLibrary } from '../../lib/api/prompts';
  import BaseModal from '../ui/BaseModal.svelte';
  import ConfirmationModal from '../ConfirmationModal.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import TagInput from '../ui/TagInput.svelte';
  import { copyToClipboard } from '$lib/utils/clipboard';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

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

  // Delete confirmation modal
  let showDeleteModal = false;
  let promptToDelete: SummaryPrompt | null = null;

  // Unsaved changes confirmation modal
  let showUnsavedChangesModal = false;
  let pendingCloseAction: (() => void) | null = null;

  // Form dirty state tracking
  let originalFormData = {};
  let isDirty = false;

  let formData: {
    name: string;
    description: string;
    prompt_text: string;
    content_type: string;
    tags: string[];
    is_shared: boolean;
  } = {
    name: '',
    description: '',
    prompt_text: '',
    content_type: 'general',
    tags: [],
    is_shared: false
  };

  $: contentTypes = [
    { value: 'general', label: $t('prompts.contentTypeGeneral') },
    { value: 'meeting', label: $t('prompts.contentTypeMeeting') },
    { value: 'interview', label: $t('prompts.contentTypeInterview') },
    { value: 'podcast', label: $t('prompts.contentTypePodcast') },
    { value: 'documentary', label: $t('prompts.contentTypeDocumentary') },
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
      const allLoaded = promptsResponse.prompts || [];
      allPrompts = allLoaded.filter(p => p.is_system_default || p.is_owner !== false);
      sharedPrompts = allLoaded.filter(p => !p.is_system_default && p.is_owner === false);

      activePrompt = activeResponse.active_prompt;
      selectedPromptId = activeResponse.active_prompt_id;

      // Auto-select universal prompt if no prompt is selected
      if (!selectedPromptId && allPrompts.length > 0) {
        const universalPrompt = allPrompts.find(p =>
          p.is_system_default &&
          (p.name.toLowerCase().includes('universal') || p.name.toLowerCase().includes('general'))
        );

        if (universalPrompt) {
          await setActivePrompt(universalPrompt.uuid);
        } else {
          // Fallback to first system prompt if no universal found
          const firstSystemPrompt = allPrompts.find(p => p.is_system_default);
          if (firstSystemPrompt) {
            await setActivePrompt(firstSystemPrompt.uuid);
          }
        }
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
      activePrompt = allPrompts.find(p => p.uuid === promptId) || sharedPrompts.find(p => p.uuid === promptId) || null;
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
    formData = {
      name: '',
      description: '',
      prompt_text: '',
      content_type: 'general',
      tags: [],
      is_shared: false
    };
    originalFormData = { ...formData, tags: [...formData.tags] };
  }

  function openEditForm(prompt: SummaryPrompt) {
    if (prompt.is_system_default) {
      toastStore.error($t('prompts.editSystemError'));
      return;
    }

    showCreateForm = true;
    editingPrompt = prompt;
    formData = {
      name: prompt.name,
      description: prompt.description || '',
      prompt_text: prompt.prompt_text,
      content_type: prompt.content_type || 'general',
      tags: [...(prompt.tags || [])],
      is_shared: prompt.is_shared || false
    };
    originalFormData = { ...formData, tags: [...formData.tags] };
  }

  function closeForm(force: boolean = false) {
    if (!force && isDirty) {
      // Show professional confirmation modal instead of browser alert
      pendingCloseAction = () => executeCloseForm();
      showUnsavedChangesModal = true;
      return;
    }

    executeCloseForm();
  }

  function executeCloseForm() {
    showCreateForm = false;
    editingPrompt = null;
    formData = {
      name: '',
      description: '',
      prompt_text: '',
      content_type: 'general',
      tags: [],
      is_shared: false
    };
    originalFormData = { ...formData, tags: [...formData.tags] };
  }

  function handleUnsavedChangesConfirm() {
    if (pendingCloseAction) {
      pendingCloseAction();
      pendingCloseAction = null;
    }
    showUnsavedChangesModal = false;
  }

  function handleUnsavedChangesCancel() {
    pendingCloseAction = null;
    showUnsavedChangesModal = false;
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

    promptToDelete = prompt;
    showDeleteModal = true;
  }

  async function deletePrompt() {
    if (!promptToDelete) return;
    const prompt = promptToDelete;

    saving = true;

    try {
      await PromptsApi.deletePrompt(prompt.uuid);

      // Remove from list
      allPrompts = allPrompts.filter(p => p.uuid !== prompt.uuid);

      // Check if we deleted the active prompt
      const wasActivePrompt = selectedPromptId === prompt.uuid;

      // Check if we have any remaining user prompts after deletion
      const remainingUserPrompts = allPrompts.filter(p => !p.is_system_default);

      // If no user prompts remain, automatically activate Universal Content Analyzer (regardless of which prompt was active)
      if (remainingUserPrompts.length === 0) {
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
      } else if (wasActivePrompt) {
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
      promptToDelete = null;
      showDeleteModal = false;
    }
  }

  async function handleShareToggle(prompt: SummaryPrompt) {
    if (prompt.is_system_default) return;
    const newShared = !prompt.is_shared;
    const idx = allPrompts.findIndex(p => p.uuid === prompt.uuid);
    const prevSharedAt = prompt.shared_at;

    // Optimistic update — flip toggle immediately for responsive UI
    if (idx !== -1) {
      allPrompts[idx] = {
        ...allPrompts[idx],
        is_shared: newShared,
        shared_at: newShared ? new Date().toISOString() : undefined
      };
      allPrompts = allPrompts;
    }

    saving = true;
    try {
      await PromptsApi.sharePrompt(prompt.uuid, newShared);
      toastStore.success(
        newShared ? $t('prompts.shareEnabled') : $t('prompts.shareDisabled')
      );
    } catch (err: unknown) {
      // Rollback on failure
      if (idx !== -1) {
        allPrompts[idx] = {
          ...allPrompts[idx],
          is_shared: !newShared,
          shared_at: prevSharedAt
        };
        allPrompts = allPrompts;
      }
      console.error('Error toggling share:', err);
      toastStore.error(getErrorMessage(err, $t('prompts.shareFailed')));
    } finally {
      saving = false;
    }
  }

  // Separate prompts by type - exclude speaker_identification from UI
  $: systemPrompts = allPrompts.filter(p => p.is_system_default && p.content_type !== 'speaker_identification');
  $: userPrompts = allPrompts.filter(p => !p.is_system_default);

  function viewPrompt(prompt: SummaryPrompt) {
    viewingPrompt = prompt;
    showViewModal = true;
  }

  function closeViewModal() {
    showViewModal = false;
    viewingPrompt = null;
  }

  $: isFormValid = !!(formData.name && formData.name.trim() && formData.prompt_text && formData.prompt_text.trim());

  // Track form changes for dirty state (tags is an array, so JSON compare works)
  $: isDirty = JSON.stringify({ ...formData, tags: formData.tags }) !== JSON.stringify(originalFormData);

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

  // Copy functionality - matches SummaryModal pattern
  let copyButtonText = 'Copy';
  let isCopied = false;

  function copyPromptText(text: string) {
    if (!text) return;

    copyToClipboard(
      text,
      () => {
        isCopied = true;
        setTimeout(() => {
          isCopied = false;
        }, 2000);
      },
      (error) => {
        // Failed - just keep default state
      }
    );
  }


</script>

<div class="prompt-settings">
  {#if loading}
    <div class="loading">{$t('prompts.loading')}</div>
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
          {#each systemPrompts as prompt}
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
                    {#each prompt.linked_collections as col}
                      <span class="collection-tag">{col.name}</span>
                    {/each}
                  </div>
                {/if}
              </div>
              <div class="prompt-actions">
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
                    class="activate-button"
                    on:click={() => setActivePrompt(prompt.uuid)}
                    disabled={saving}
                    title={$t('prompts.makeActive')}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="3"/>
                      <circle cx="12" cy="1" r="1"/>
                      <circle cx="12" cy="23" r="1"/>
                      <circle cx="4.22" cy="4.22" r="1"/>
                      <circle cx="19.78" cy="19.78" r="1"/>
                      <circle cx="1" cy="12" r="1"/>
                      <circle cx="23" cy="12" r="1"/>
                      <circle cx="4.22" cy="19.78" r="1"/>
                      <circle cx="19.78" cy="4.22" r="1"/>
                    </svg>
                    {$t('prompts.activate')}
                  </button>
                {/if}

                <button
                  class="view-button"
                  on:click={() => viewPrompt(prompt)}
                  title={$t('prompts.viewPrompt')}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                    <circle cx="12" cy="12" r="3"/>
                  </svg>
                </button>
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
          <button class="create-config-button" on:click={openCreateForm} title={$t('prompts.createNew')}>
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
          {#each userPrompts as prompt}
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
                    {#each prompt.tags as tag}
                      <span class="tag-pill">{tag}</span>
                    {/each}
                  </div>
                {/if}
                {#if prompt.linked_collections && prompt.linked_collections.length > 0}
                  <div class="linked-collections">
                    <span class="linked-label">{$t('prompts.usedByCollections')}:</span>
                    {#each prompt.linked_collections as col}
                      <span class="collection-tag">{col.name}</span>
                    {/each}
                  </div>
                {/if}
                <div class="share-toggle-row">
                  <label class="toggle-label">
                    <input type="checkbox" class="toggle-input" checked={prompt.is_shared}
                      on:change={() => handleShareToggle(prompt)} disabled={saving} />
                    <span class="toggle-switch"></span>
                    <span class="toggle-text">{$t('prompts.shareGlobally')}</span>
                  </label>
                </div>
              </div>
              <div class="prompt-actions">
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
                    class="activate-button"
                    on:click={() => setActivePrompt(prompt.uuid)}
                    disabled={saving}
                    title={$t('prompts.makeActive')}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="3"/>
                      <circle cx="12" cy="1" r="1"/>
                      <circle cx="12" cy="23" r="1"/>
                      <circle cx="4.22" cy="4.22" r="1"/>
                      <circle cx="19.78" cy="19.78" r="1"/>
                      <circle cx="1" cy="12" r="1"/>
                      <circle cx="23" cy="12" r="1"/>
                      <circle cx="4.22" cy="19.78" r="1"/>
                      <circle cx="19.78" cy="4.22" r="1"/>
                    </svg>
                    {$t('prompts.activate')}
                  </button>
                {/if}

                <button
                  class="view-button"
                  on:click={() => viewPrompt(prompt)}
                  title={$t('prompts.viewPrompt')}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                    <circle cx="12" cy="12" r="3"/>
                  </svg>
                </button>
                <button
                  class="edit-button"
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
                  class="delete-config-button"
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
        <div class="empty-state">
          <div class="empty-icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14,2 14,8 20,8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
              <polyline points="10,9 9,9 8,9"/>
            </svg>
          </div>
          <h4>{$t('prompts.noCustomPrompts')}</h4>
          <p>{$t('prompts.noCustomPromptsDesc')}</p>
          <button class="create-first-config-btn" on:click={openCreateForm}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19"/>
              <line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            {$t('prompts.createFirstPrompt')}
          </button>
        </div>
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
          {#each sharedPrompts as prompt}
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
                    {#each prompt.tags as tag}
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
                    class="activate-button"
                    on:click={() => setActivePrompt(prompt.uuid)}
                    disabled={saving}
                    title={$t('prompts.makeActive')}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="3"/>
                      <circle cx="12" cy="1" r="1"/>
                      <circle cx="12" cy="23" r="1"/>
                      <circle cx="4.22" cy="4.22" r="1"/>
                      <circle cx="19.78" cy="19.78" r="1"/>
                      <circle cx="1" cy="12" r="1"/>
                      <circle cx="23" cy="12" r="1"/>
                      <circle cx="4.22" cy="19.78" r="1"/>
                      <circle cx="19.78" cy="4.22" r="1"/>
                    </svg>
                    {$t('prompts.activate')}
                  </button>
                {/if}

                <button
                  class="clone-button"
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

                <button
                  class="view-button"
                  on:click={() => viewPrompt(prompt)}
                  title={$t('prompts.viewPrompt')}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                    <circle cx="12" cy="12" r="3"/>
                  </svg>
                </button>
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
    maxWidth="600px"
  >
    <svelte:fragment slot="header">
      <h2 class="modal-title">{editingPrompt ? $t('prompts.editExisting') : $t('prompts.createNew')}</h2>
      {#if isDirty}
        <span class="unsaved-dot" title={$t('prompts.unsavedChanges')}>●</span>
      {/if}
    </svelte:fragment>
    <form id="prompt-form" on:submit|preventDefault={savePrompt} class="prompt-form">
      <div class="form-group">
        <label for="name">{$t('prompts.promptName')}</label>
        <input
          type="text"
          id="name"
          bind:value={formData.name}
          disabled={saving}
          class="form-control"
          placeholder={$t('prompts.promptNamePlaceholder')}
          required
        />
      </div>

      <div class="form-group">
        <label for="content_type">{$t('prompts.contentType')}</label>
        <select
          id="content_type"
          bind:value={formData.content_type}
          disabled={saving}
          class="form-control"
        >
          {#each contentTypes as type}
            <option value={type.value}>{type.label}</option>
          {/each}
        </select>
      </div>

      <div class="form-group">
        <label for="description">{$t('prompts.descriptionLabel')}</label>
        <input
          type="text"
          id="description"
          bind:value={formData.description}
          disabled={saving}
          class="form-control"
          placeholder={$t('prompts.descriptionPlaceholder')}
        />
      </div>

      <div class="form-group">
        <label for="tags">{$t('prompts.tags')}</label>
        <TagInput id="tags" bind:tags={formData.tags} placeholder={$t('prompts.tagsPlaceholder')} disabled={saving} />
        <small class="form-text">{$t('prompts.tagsHelp')}</small>
      </div>

      <!-- Share with all users -->
      <div class="share-toggle-row">
        <label class="toggle-label">
          <input type="checkbox" class="toggle-input" bind:checked={formData.is_shared} disabled={saving} />
          <span class="toggle-switch"></span>
          <span class="toggle-text">{$t('prompts.shareGlobally')}</span>
        </label>
      </div>

      <div class="form-group">
        <div class="textarea-header">
          <label for="prompt_text">{$t('prompts.promptText')}</label>
          {#if formData.prompt_text.trim()}
            <button
              type="button"
              class="copy-button-header"
              class:copied={isCopied}
              on:click={() => copyPromptText(formData.prompt_text)}
              aria-label={$t('prompts.copy')}
              title={isCopied ? $t('prompts.copiedToClipboard') : $t('prompts.copy')}
            >
              {#if isCopied}
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M10.97 4.97a.75.75 0 0 1 1.07 1.05l-3.99 4.99a.75.75 0 0 1-1.08.02L4.324 8.384a.75.75 0 1 1 1.06-1.06l2.094 2.093 3.473-4.425a.267.267 0 0 1 .02-.022z"/>
                </svg>
                {$t('prompts.copied')}
              {:else}
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M4 1.5H3a2 2 0 0 0-2 2V14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V3.5a2 2 0 0 0-2-2h-1v1h1a1 1 0 0 1 1 1V14a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1h1v-1z"/>
                  <path d="M9.5 1a.5.5 0 0 1 .5.5v1a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-1a.5.5 0 0 1 .5-.5h3zm-3-1A1.5 1.5 0 0 0 5 1.5v1A1.5 1.5 0 0 0 6.5 4h3A1.5 1.5 0 0 0 11 2.5v-1A1.5 1.5 0 0 0 9.5 0h-3z"/>
                </svg>
                {$t('prompts.copy')}
              {/if}
            </button>
          {/if}
        </div>
        <textarea
          id="prompt_text"
          bind:value={formData.prompt_text}
          disabled={saving}
          class="form-control textarea"
          rows="8"
          placeholder={$t('prompts.promptTextPlaceholder')}
          required
        ></textarea>
        <small class="form-text">
          {$t('prompts.promptTextHelp')}
        </small>
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
      </div>
    </form>
    <svelte:fragment slot="footer">
      <button type="button" class="modal-button modal-cancel-button"
              on:click={() => closeForm()} disabled={saving}>
        {$t('prompts.cancel')}
      </button>
      <button type="submit" form="prompt-form"
              class="modal-button modal-primary-button"
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
    maxWidth="700px"
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
              {#each viewingPrompt.linked_collections as col}
                <span class="collection-tag">{col.name}</span>
              {/each}
            </div>
          </div>
        {/if}
      </div>
      <div class="prompt-text-container">
        <div class="prompt-text-header">
          <strong>{$t('prompts.promptTextLabel')}:</strong>
          <button
            type="button"
            class="copy-button-header"
            class:copied={isCopied}
            on:click={() => copyPromptText(viewingPrompt?.prompt_text ?? '')}
            aria-label={$t('prompts.copy')}
            title={isCopied ? $t('prompts.copiedToClipboard') : $t('prompts.copy')}
          >
            {#if isCopied}
              <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                <path d="M10.97 4.97a.75.75 0 0 1 1.07 1.05l-3.99 4.99a.75.75 0 0 1-1.08.02L4.324 8.384a.75.75 0 1 1 1.06-1.06l2.094 2.093 3.473-4.425a.267.267 0 0 1 .02-.022z"/>
              </svg>
              {$t('prompts.copied')}
            {:else}
              <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                <path d="M4 1.5H3a2 2 0 0 0-2 2V14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V3.5a2 2 0 0 0-2-2h-1v1h1a1 1 0 0 1 1 1V14a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1h1v-1z"/>
                <path d="M9.5 1a.5.5 0 0 1 .5.5v1a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-1a.5.5 0 0 1 .5-.5h3zm-3-1A1.5 1.5 0 0 0 5 1.5v1A1.5 1.5 0 0 0 6.5 4h3A1.5 1.5 0 0 0 11 2.5v-1A1.5 1.5 0 0 0 9.5 0h-3z"/>
              </svg>
              {$t('prompts.copy')}
            {/if}
          </button>
        </div>
        <div class="prompt-text-display">{viewingPrompt.prompt_text}</div>
      </div>
    {/if}
  </BaseModal>

  <!-- Delete Confirmation Modal -->
  <ConfirmationModal
    bind:isOpen={showDeleteModal}
    title={$t('prompts.deleteConfirmTitle')}
    message={promptToDelete ? $t('prompts.deleteConfirmMessage', { name: promptToDelete.name }) : ''}
    confirmText={$t('prompts.delete')}
    cancelText={$t('prompts.cancel')}
    confirmButtonClass="modal-delete-button"
    cancelButtonClass="modal-cancel-button"
    on:confirm={deletePrompt}
    on:cancel={() => { promptToDelete = null; showDeleteModal = false; }}
    on:close={() => { promptToDelete = null; showDeleteModal = false; }}
  />

  <!-- Unsaved Changes Confirmation Modal -->
  <ConfirmationModal
    bind:isOpen={showUnsavedChangesModal}
    title={$t('prompts.unsavedChanges')}
    message={$t('prompts.unsavedChangesMessage')}
    confirmText={$t('prompts.discardChanges')}
    cancelText={$t('prompts.keepEditing')}
    confirmButtonClass="modal-warning-button"
    cancelButtonClass="modal-primary-button"
    on:confirm={handleUnsavedChangesConfirm}
    on:cancel={handleUnsavedChangesCancel}
    on:close={handleUnsavedChangesCancel}
  />
</div>

<style>
  .prompt-settings {
    max-width: 800px;
  }


  .loading {
    text-align: center;
    padding: 2rem;
    color: var(--text-light);
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }

  .prompt-actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    align-items: center;
  }


  /* New button styles to match LLM provider page */
  .create-config-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .create-config-button:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .create-config-button:active {
    transform: scale(1);
  }

  .config-status {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.375rem 0.75rem;
    border-radius: 4px;
    font-size: 0.8rem;
    font-weight: 500;
    width: fit-content;
  }

  .config-status.currently-active {
    background-color: var(--success-bg);
    color: var(--success-color);
    border: 1px solid var(--success-border);
  }

  .status-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .activate-button, .view-button, .edit-button, .delete-config-button {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    font-size: 0.8rem;
    cursor: pointer;
    transition: all 0.2s ease;
    border: 1px solid;
    height: 32px;
    box-sizing: border-box;
  }

  .activate-button {
    background-color: var(--success-color);
    border-color: var(--success-color);
    color: white;
    padding: 0.5rem 0.5rem;
  }

  .activate-button:hover:not(:disabled) {
    background-color: #059669;
    border-color: #059669;
  }

  .view-button {
    background-color: transparent;
    border-color: var(--primary-color);
    color: var(--primary-color);
  }

  .view-button:hover:not(:disabled) {
    background-color: #3b82f6;
    border-color: var(--primary-color);
    color: white;
  }

  .edit-button {
    background-color: transparent;
    border-color: var(--border-color);
    color: var(--text-color);
  }

  .edit-button:hover:not(:disabled) {
    background-color: #3b82f6;
    border-color: var(--primary-color);
    color: white;
  }

  .delete-config-button {
    background-color: transparent;
    border-color: var(--error-color);
    color: var(--error-color);
  }

  .delete-config-button:hover:not(:disabled) {
    background-color: var(--error-color);
    border-color: var(--error-color);
    color: white;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(239, 68, 68, 0.25);
  }

  .delete-config-button:active:not(:disabled) {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2);
  }

  .saved-configs-section {
    margin-bottom: 2rem;
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .section-header h4 {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0;
    font-size: 1.125rem;
    font-weight: 500;
    color: var(--text-color);
  }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Additional styles to match LLM provider structure */
  .config-list {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .config-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--card-bg);
    transition: all 0.2s ease;
  }

  .config-item.active {
    border-color: var(--primary-color);
    background: var(--primary-bg);
  }

  .config-item:hover:not(.active) {
    border-color: var(--border-hover);
    background: var(--hover-color);
  }

  .config-info {
    flex: 1;
    min-width: 0;
  }

  .config-name {
    font-weight: 500;
    color: var(--text-color);
    font-size: 0.8125rem;
    margin-bottom: 0.25rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .info-tooltip {
    display: inline-flex;
    align-items: center;
    color: var(--text-muted);
    opacity: 0.6;
    cursor: help;
    transition: opacity 0.2s ease;
    position: relative;
  }

  .info-tooltip:hover {
    opacity: 1;
    color: var(--primary-color);
  }

  .info-tooltip[data-tooltip]:hover::after {
    content: attr(data-tooltip);
    position: fixed;
    left: var(--tooltip-left, 50%);
    top: var(--tooltip-top, 50%);
    background: #1a1a1a;
    color: white;
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: normal;
    max-width: 320px;
    white-space: normal;
    z-index: 9999;
    line-height: 1.4;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
    text-align: left;
    pointer-events: none;
    transform: translate(-50%, 8px);
  }


  .config-provider {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin-bottom: 0.25rem;
  }

  .config-url {
    font-size: 0.75rem;
    color: var(--text-muted);
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
    gap: 0.35rem;
    margin-top: 0.4rem;
    font-size: 0.75rem;
  }

  .linked-label {
    color: var(--text-muted);
    font-size: 0.7rem;
    white-space: nowrap;
  }

  .collection-tag {
    display: inline-flex;
    align-items: center;
    padding: 0.125rem 0.5rem;
    background: rgba(59, 130, 246, 0.1);
    color: var(--primary-color);
    border-radius: 10px;
    font-size: 0.7rem;
    font-weight: 500;
    white-space: nowrap;
  }

  .linked-collections-modal {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-top: 0.25rem;
  }

  :global(.dark) .collection-tag {
    background: rgba(var(--primary-color-rgb), 0.2);
    color: #60a5fa;
  }

  .empty-state {
    text-align: center;
    padding: 3rem 2rem;
    border: 2px dashed var(--border-color);
    border-radius: 8px;
    background: var(--card-bg);
  }

  .empty-icon {
    margin-bottom: 1rem;
    color: var(--text-muted);
    opacity: 0.6;
  }

  .empty-state h4 {
    margin: 0 0 0.5rem;
    color: var(--text-color);
    font-size: 1.125rem;
    font-weight: 500;
  }

  .empty-state p {
    margin: 0 0 1.5rem;
    color: var(--text-muted);
    font-size: 0.8125rem;
    line-height: 1.5;
  }

  .create-first-config-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    background: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .create-first-config-btn:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .create-first-config-btn:active {
    transform: scale(1);
  }

  /* Copy button styles - matches SummaryModal */
  .textarea-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .prompt-text-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
  }

  .copy-button-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
    font-size: 0.8125rem;
  }

  .copy-button-header:hover {
    background-color: var(--hover-bg);
    color: var(--primary-color);
    border-color: var(--primary-color);
  }

  .copy-button-header.copied {
    background-color: var(--success-bg);
    border-color: var(--success-color);
    color: var(--success-color);
  }

  .copy-button-header.copied:hover {
    background-color: var(--success-bg);
    border-color: var(--success-color);
    color: var(--success-color);
  }

  .prompt-form {
    padding: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  label {
    font-weight: 500;
    font-size: 0.8125rem;
    color: var(--text-color);
  }

  .form-control {
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-size: 0.8125rem;
    background-color: var(--background-color);
    color: var(--text-color);
    transition: border-color 0.2s ease;
  }

  .form-control.textarea {
    resize: vertical;
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
    font-size: 0.8rem;
    line-height: 1.5;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
  }

  .form-control:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .form-text {
    font-size: 0.8rem;
    color: var(--text-light);
  }

  @media (max-width: 768px) {
    .section-header {
      flex-direction: column;
      align-items: stretch;
      gap: 1rem;
    }

    .prompt-actions {
      justify-content: flex-start;
    }

    .prompt-actions button {
      min-height: 44px;
    }

    .create-config-button {
      width: 100%;
      justify-content: center;
      min-height: 44px;
    }
  }

  /* LLM Hint Styling */
  .llm-hint {
    margin-top: 0.75rem;
    padding: 0.75rem;
    background: var(--color-info-bg);
    border: 1px solid var(--color-info-border);
    color: var(--text-color);
    border-radius: 6px;
    font-size: 0.8rem;
    line-height: 1.3;
  }

  .llm-hint strong {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.25rem;
    color: var(--color-info);
  }

  .tip-icon {
    flex-shrink: 0;
  }

  /* View Modal Styling */
  .prompt-details {
    margin-bottom: 1.5rem;
  }

  .detail-row {
    margin-bottom: 0.75rem;
    font-size: 0.8125rem;
  }

  .detail-row strong {
    color: var(--primary-color, #3b82f6);
    margin-right: 0.5rem;
  }

  .prompt-text-container {
    margin-top: 1.5rem;
  }

  .prompt-text-container strong {
    display: block;
    margin-bottom: 0.75rem;
    color: var(--primary-color, #3b82f6);
  }

  .prompt-text-display {
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
    font-size: 0.8125rem;
    line-height: 1.6;
    color: var(--text-color);
    white-space: pre-wrap;
    word-wrap: break-word;
    background-color: var(--surface-color);
    padding: 1rem;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    max-height: 400px;
    overflow-y: auto;
  }

  /* Shared config styles */
  .config-item.shared {
    border-left: 3px solid var(--info-color, #3b82f6);
    background: rgba(59, 130, 246, 0.04);
  }

  :global([data-theme='dark']) .config-item.shared,
  :global(.dark) .config-item.shared {
    background: rgba(96, 165, 250, 0.06);
  }

  .share-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 10px;
    font-size: 0.625rem;
    font-weight: 600;
    background: rgba(59, 130, 246, 0.12);
    color: var(--primary-color);
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }

  :global([data-theme='dark']) .share-badge,
  :global(.dark) .share-badge {
    background: rgba(96, 165, 250, 0.15);
    color: #60a5fa;
  }

  .admin-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 10px;
    font-size: 0.625rem;
    font-weight: 600;
    background: rgba(245, 158, 11, 0.12);
    color: #d97706;
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }

  :global([data-theme='dark']) .admin-badge,
  :global(.dark) .admin-badge {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }

  .shared-by {
    font-size: 0.7rem;
    color: var(--text-muted);
    margin-top: 0.35rem;
    font-style: italic;
  }

  .shared-section-header h4 {
    color: var(--primary-color);
  }

  :global([data-theme='dark']) .shared-section-header h4,
  :global(.dark) .shared-section-header h4 {
    color: #60a5fa;
  }

  /* Tag pills */
  .tag-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
    margin-top: 0.35rem;
  }

  .tag-pill {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.6875rem;
    font-weight: 500;
    background: rgba(59, 130, 246, 0.12);
    color: var(--primary-color);
  }

  :global([data-theme='dark']) .tag-pill,
  :global(.dark) .tag-pill {
    background: rgba(var(--primary-color-rgb), 0.2);
    color: #60a5fa;
  }

  /* Share toggle */
  .share-toggle-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-top: 0.5rem;
    padding-top: 0.5rem;
    border-top: 1px dashed var(--border-color);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-weight: 400;
  }

  .toggle-input {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-switch {
    position: relative;
    width: 36px;
    height: 20px;
    background-color: var(--border-color);
    border-radius: 10px;
    transition: background-color 0.2s ease;
    flex-shrink: 0;
  }

  .toggle-switch::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    background-color: white;
    border-radius: 50%;
    transition: transform 0.2s ease;
  }

  .toggle-input:checked + .toggle-switch {
    background-color: #3b82f6;
  }

  .toggle-input:checked + .toggle-switch::after {
    transform: translateX(16px);
  }

  .toggle-text {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .modal-title {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .unsaved-dot {
    color: var(--warning-color);
    font-size: 0.9rem;
    line-height: 1;
    flex-shrink: 0;
  }
</style>
