<!--
  ProjectModal.svelte — create or edit a chat project (issue #360).

  A project is more than a folder: it pins a transcript scope every chat inside
  inherits, and a prompt layer that carries standing background about the client
  or meeting. Both are edited here.

  Deleting is deliberately worded as "the conversations stay" — the backend FK is
  ON DELETE SET NULL, and people hesitate to tidy up if they suspect a delete
  takes the threads with it.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import FilePickerModal from './FilePickerModal.svelte';
  import { createProject, deleteProject, updateProject } from '$lib/api/chatApi';
  import { emptyScope, isScopeEmpty } from '$lib/types/chat';
  import type { ChatProjectDetail, ChatScope } from '$lib/types/chat';

  /** null = create mode. */
  export let project: ChatProjectDetail | null = null;
  export let isOpen = false;

  const MAX_PROMPT = 2000;

  const dispatch = createEventDispatcher<{
    saved: ChatProjectDetail;
    deleted: string;
    close: void;
  }>();

  let name = '';
  let description = '';
  let systemPrompt = '';
  let scope: ChatScope = emptyScope();
  let saving = false;
  let error = '';
  let confirmingDelete = false;
  let pickerOpen = false;

  // Reload the form whenever a different project is opened.
  $: if (isOpen) hydrate(project);

  function hydrate(source: ChatProjectDetail | null): void {
    name = source?.name ?? '';
    description = source?.description ?? '';
    systemPrompt = source?.system_prompt ?? '';
    scope = source?.scope ?? emptyScope();
    error = '';
    confirmingDelete = false;
  }

  $: scopeCount =
    scope.file_uuids.length + scope.collection_uuids.length + scope.tag_names.length;

  async function save(): Promise<void> {
    if (!name.trim()) {
      error = $t('chat.projects.nameRequired');
      return;
    }
    saving = true;
    error = '';
    try {
      const payload = {
        name: name.trim(),
        description: description.trim() || null,
        // "" clears the layer server-side; null would mean "leave unchanged".
        system_prompt: systemPrompt.trim() || '',
        scope,
      };
      const saved = project
        ? await updateProject(project.uuid, payload)
        : await createProject(payload);
      dispatch('saved', saved);
      dispatch('close');
    } catch {
      error = $t('chat.projects.saveError');
    } finally {
      saving = false;
    }
  }

  async function remove(): Promise<void> {
    if (!project) return;
    saving = true;
    try {
      await deleteProject(project.uuid);
      dispatch('deleted', project.uuid);
      dispatch('close');
    } catch {
      error = $t('chat.projects.deleteError');
    } finally {
      saving = false;
    }
  }
</script>

<BaseModal
  {isOpen}
  title={project ? $t('chat.projects.editTitle') : $t('chat.projects.newTitle')}
  maxWidth="560px"
  onClose={() => dispatch('close')}
>
  <div class="project-form" data-testid="chat-project-modal">
    <div class="field">
      <label class="field-label" for="project-name">{$t('chat.projects.name')}</label>
      <input
        id="project-name"
        bind:value={name}
        maxlength="120"
        placeholder={$t('chat.projects.namePlaceholder')}
        data-testid="chat-project-name"
      />
    </div>

    <div class="field">
      <label class="field-label" for="project-description">
        {$t('chat.projects.description')}
      </label>
      <input
        id="project-description"
        bind:value={description}
        maxlength="1000"
        placeholder={$t('chat.projects.descriptionPlaceholder')}
      />
    </div>

    <div class="field">
      <span class="field-label">{$t('chat.projects.scope')}</span>
      <button
        type="button"
        class="scope-button"
        on:click={() => (pickerOpen = true)}
        data-testid="chat-project-scope"
      >
        {isScopeEmpty(scope)
          ? $t('chat.projects.scopeAll')
          : $t('chat.projects.scopeCount', { count: scopeCount })}
      </button>
      <span class="hint">{$t('chat.projects.scopeHint')}</span>
    </div>

    <div class="field">
      <label class="field-label" for="project-prompt">{$t('chat.projects.instructions')}</label>
      <textarea
        id="project-prompt"
        bind:value={systemPrompt}
        rows="4"
        maxlength={MAX_PROMPT}
        placeholder={$t('chat.projects.instructionsPlaceholder')}
        data-testid="chat-project-prompt"
      ></textarea>
      <div class="field-footer">
        <span class="hint">{$t('chat.projects.instructionsHint')}</span>
        <span class="char-count">{systemPrompt.length} / {MAX_PROMPT}</span>
      </div>
    </div>

    {#if error}
      <p class="error" role="alert">{error}</p>
    {/if}
  </div>

  <svelte:fragment slot="footer">
    {#if project && !confirmingDelete}
      <button
        type="button"
        class="modal-button delete-button"
        on:click={() => (confirmingDelete = true)}
        data-testid="chat-project-delete"
      >
        {$t('common.delete')}
      </button>
    {/if}
    {#if confirmingDelete}
      <span class="confirm-text">{$t('chat.projects.deleteConfirm')}</span>
      <button type="button" class="modal-button delete-button" on:click={remove} disabled={saving}>
        {$t('common.delete')}
      </button>
      <button type="button" class="modal-button modal-cancel-button" on:click={() => (confirmingDelete = false)}>
        {$t('common.cancel')}
      </button>
    {:else}
      <button
        type="button"
        class="modal-button modal-cancel-button"
        on:click={() => dispatch('close')}
      >
        {$t('common.cancel')}
      </button>
      <button
        type="button"
        class="modal-button modal-primary-button"
        on:click={save}
        disabled={saving || !name.trim()}
        data-testid="chat-project-save"
      >
        {saving ? $t('common.saving') : $t('common.save')}
      </button>
    {/if}
  </svelte:fragment>
</BaseModal>

<FilePickerModal
  isOpen={pickerOpen}
  {scope}
  on:confirm={(e) => {
    scope = e.detail;
    pickerOpen = false;
  }}
  on:close={() => (pickerOpen = false)}
/>

<style>
  .project-form {
    display: flex;
    flex-direction: column;
    gap: 1.1rem;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .field-label {
    font-size: 0.87rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .scope-button {
    align-self: flex-start;
    padding: 0.45rem 0.8rem;
    border: 1px solid var(--border-color);
    border-radius: 999px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.85rem;
    box-shadow: none;
    cursor: pointer;
  }

  .scope-button:hover {
    border-color: var(--primary-color);
    background-color: rgba(var(--primary-color-rgb), 0.08);
    transform: none;
    box-shadow: none;
  }

  textarea {
    width: 100%;
    padding: 0.5rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-family: inherit;
    font-size: 0.88rem;
    resize: vertical;
  }

  .field-footer {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 1rem;
  }

  .hint {
    font-size: 0.78rem;
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .char-count {
    flex-shrink: 0;
    font-size: 0.75rem;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .confirm-text {
    margin-right: auto;
    font-size: 0.82rem;
    color: var(--text-secondary);
  }

  .error {
    margin: 0;
    font-size: 0.82rem;
    color: var(--error-color, #dc3545);
  }
</style>
