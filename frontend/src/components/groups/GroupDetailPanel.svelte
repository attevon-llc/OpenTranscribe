<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { GroupDetail } from '$lib/types/groups';
  import { GroupsApi } from '$lib/api/groups';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { formatDate } from '$lib/utils/formatting';
  import GroupRoleBadge from './GroupRoleBadge.svelte';
  import GroupMemberList from './GroupMemberList.svelte';
  import GroupMemberSearch from './GroupMemberSearch.svelte';
  import ConfirmationModal from '../ConfirmationModal.svelte';

  export let group: GroupDetail;

  const dispatch = createEventDispatcher<{
    back: void;
    deleted: { uuid: string };
    updated: void;
    left: void;
  }>();

  let editingName = false;
  let editingDescription = false;
  let editName = group.name;
  let editDescription = group.description || '';
  let isSaving = false;
  let isDeleting = false;
  let showDeleteConfirm = false;

  // Keep edit fields in sync when the group prop changes externally
  $: if (!editingName) editName = group.name;
  $: if (!editingDescription) editDescription = group.description || '';

  $: canEdit = group.my_role === 'owner' || group.my_role === 'admin';
  $: canDelete = group.my_role === 'owner';
  $: canAddMembers = group.my_role === 'owner' || group.my_role === 'admin';
  $: memberUuids = group.members.map((m) => m.user_uuid);

  function startEditName() {
    if (!canEdit) return;
    editName = group.name;
    editingName = true;
  }

  function startEditDescription() {
    if (!canEdit) return;
    editDescription = group.description || '';
    editingDescription = true;
  }

  function cancelEditName() {
    editingName = false;
    editName = group.name;
  }

  function cancelEditDescription() {
    editingDescription = false;
    editDescription = group.description || '';
  }

  async function saveName() {
    if (!editName.trim() || editName.trim() === group.name) {
      cancelEditName();
      return;
    }

    isSaving = true;
    try {
      const updated = await GroupsApi.updateGroup(group.uuid, { name: editName.trim() });
      group = { ...group, name: updated.name };
      editingName = false;
      toastStore.success($t('groups.toast.groupUpdated'));
      dispatch('updated');
    } catch (err: unknown) {
      const message = getErrorMessage(err, $t('groups.toast.updateGroupFailed'));
      toastStore.error(message);
    } finally {
      isSaving = false;
    }
  }

  async function saveDescription() {
    const newDesc = editDescription.trim();
    if (newDesc === (group.description || '')) {
      cancelEditDescription();
      return;
    }

    isSaving = true;
    try {
      const updated = await GroupsApi.updateGroup(group.uuid, { description: newDesc || undefined });
      group = { ...group, description: updated.description ?? null };
      editingDescription = false;
      toastStore.success($t('groups.toast.groupUpdated'));
      dispatch('updated');
    } catch (err: unknown) {
      const message = getErrorMessage(err, $t('groups.toast.updateGroupFailed'));
      toastStore.error(message);
    } finally {
      isSaving = false;
    }
  }

  function requestDelete() {
    showDeleteConfirm = true;
  }

  async function executeDelete() {
    showDeleteConfirm = false;
    isDeleting = true;
    try {
      await GroupsApi.deleteGroup(group.uuid);
      toastStore.success($t('groups.toast.groupDeleted'));
      dispatch('deleted', { uuid: group.uuid });
    } catch (err: unknown) {
      const message = getErrorMessage(err, $t('groups.toast.deleteGroupFailed'));
      toastStore.error(message);
    } finally {
      isDeleting = false;
    }
  }

  async function refreshGroup() {
    try {
      const updated = await GroupsApi.fetchGroupDetail(group.uuid);
      group = updated;
    } catch (err: unknown) {
      console.error('Failed to refresh group:', err);
    }
  }

  function handleMemberAdded() {
    refreshGroup();
  }

  function handleMemberRemoved() {
    refreshGroup();
  }

  function handleRoleChanged() {
    refreshGroup();
  }

  function handleLeft() {
    dispatch('left');
  }

  function handleNameKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      saveName();
    } else if (event.key === 'Escape') {
      cancelEditName();
    }
  }

  function handleDescKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      cancelEditDescription();
    }
  }

</script>

<div class="group-detail">
  <!-- Header with back button -->
  <div class="detail-header">
    <button class="btn-back" on:click={() => dispatch('back')}>
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="19" y1="12" x2="5" y2="12"></line>
        <polyline points="12 19 5 12 12 5"></polyline>
      </svg>
      <span>{$t('groups.backToGroups')}</span>
    </button>

    {#if canDelete}
      <button
        class="btn-delete"
        on:click={requestDelete}
        disabled={isDeleting}
        title={$t('groups.deleteGroup')}
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="3 6 5 6 21 6"></polyline>
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
        </svg>
        {isDeleting ? $t('groups.deleting') : $t('groups.deleteGroup')}
      </button>
    {/if}
  </div>

  <!-- Group name -->
  <div class="detail-name-section">
    {#if editingName}
      <div class="edit-inline">
        <input
          type="text"
          class="form-control edit-name-input"
          bind:value={editName}
          maxlength="255"
          on:keydown={handleNameKeydown}
        />
        <div class="edit-actions">
          <button class="btn-inline-save" on:click={saveName} disabled={isSaving}>
            {$t('common.save')}
          </button>
          <button class="btn-inline-cancel" on:click={cancelEditName}>
            {$t('modal.cancel')}
          </button>
        </div>
      </div>
    {:else}
      <div class="display-name" class:editable={canEdit} role="button" tabindex="0" title={canEdit ? $t('groups.clickToEdit') : undefined} on:click={startEditName} on:keydown={(e) => (e.key === 'Enter' || e.key === ' ') && startEditName()}>
        <h3 class="group-name">{group.name}</h3>
        <GroupRoleBadge role={group.my_role} />
        {#if canEdit}
          <svg class="edit-icon" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
          </svg>
        {/if}
      </div>
    {/if}
  </div>

  <!-- Description -->
  <div class="detail-description-section">
    {#if editingDescription}
      <div class="edit-inline">
        <textarea
          class="form-control edit-desc-textarea"
          bind:value={editDescription}
          rows="3"
          on:keydown={handleDescKeydown}
        ></textarea>
        <div class="edit-actions">
          <button class="btn-inline-save" on:click={saveDescription} disabled={isSaving}>
            {$t('common.save')}
          </button>
          <button class="btn-inline-cancel" on:click={cancelEditDescription}>
            {$t('modal.cancel')}
          </button>
        </div>
      </div>
    {:else}
      <div class="display-description" class:editable={canEdit} role="button" tabindex="0" title={canEdit ? $t('groups.clickToEdit') : undefined} on:click={startEditDescription} on:keydown={(e) => (e.key === 'Enter' || e.key === ' ') && startEditDescription()}>
        {#if group.description}
          <p class="group-description">{group.description}</p>
        {:else if canEdit}
          <p class="group-description placeholder">{$t('groups.addDescription')}</p>
        {/if}
      </div>
    {/if}
  </div>

  <!-- Meta info -->
  <div class="detail-meta">
    <span class="meta-item">
      {$t('groups.memberCount', { count: group.member_count })}
    </span>
    <span class="meta-separator">-</span>
    <span class="meta-item">
      {$t('groups.createdOn')} {formatDate(group.created_at)}
    </span>
    <span class="meta-separator">-</span>
    <span class="meta-item">
      {$t('groups.owner')}: {group.owner.full_name || group.owner.email}
    </span>
  </div>

  <div class="section-divider"></div>

  <!-- Add Members -->
  {#if canAddMembers}
    <GroupMemberSearch
      groupUuid={group.uuid}
      existingMemberUuids={memberUuids}
      on:memberAdded={handleMemberAdded}
    />

    <div class="section-divider"></div>
  {/if}

  <!-- Members List -->
  <div class="members-section">
    <h4 class="members-title">{$t('groups.members')}</h4>
    <GroupMemberList
      members={group.members}
      groupUuid={group.uuid}
      myRole={group.my_role}
      on:memberRemoved={handleMemberRemoved}
      on:roleChanged={handleRoleChanged}
      on:left={handleLeft}
    />
  </div>

  <ConfirmationModal
    bind:isOpen={showDeleteConfirm}
    title={$t('groups.deleteGroup')}
    message={$t('groups.confirmDelete')}
    confirmText={isDeleting ? $t('groups.deleting') : $t('groups.deleteGroup')}
    cancelText={$t('modal.cancel')}
    confirmButtonClass="modal-delete-button"
    on:confirm={executeDelete}
    on:cancel={() => showDeleteConfirm = false}
    on:close={() => showDeleteConfirm = false}
  />
</div>

<style>
  .group-detail {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .detail-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.25rem;
  }

  .btn-back {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    background: none;
    border: none;
    cursor: pointer;
    color: var(--primary-color);
    font-size: 0.8125rem;
    font-weight: 500;
    padding: 0.375rem 0.5rem;
    border-radius: 6px;
    transition: background-color 0.15s;
  }

  .btn-back:hover {
    background-color: var(--primary-light, rgba(59, 130, 246, 0.08));
  }

  .btn-delete {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.375rem 0.75rem;
    border: 1px solid var(--danger-color, #dc2626);
    border-radius: 6px;
    background: transparent;
    color: var(--danger-color, #dc2626);
    font-size: 0.75rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
  }

  .btn-delete:hover:not(:disabled) {
    background-color: rgba(220, 38, 38, 0.08);
  }

  .btn-delete:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .detail-name-section {
    margin-bottom: 0.125rem;
  }

  .display-name {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .display-name.editable {
    cursor: pointer;
    padding: 0.25rem 0.5rem;
    margin: -0.25rem -0.5rem;
    border-radius: 6px;
    transition: background-color 0.15s;
  }

  .display-name.editable:hover {
    background-color: var(--background-color);
  }

  .group-name {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .edit-icon {
    color: var(--text-secondary);
    opacity: 0;
    transition: opacity 0.15s;
  }

  .display-name.editable:hover .edit-icon {
    opacity: 1;
  }

  .detail-description-section {
    min-height: 1.5rem;
  }

  .display-description.editable {
    cursor: pointer;
    padding: 0.25rem 0.5rem;
    margin: -0.25rem -0.5rem;
    border-radius: 6px;
    transition: background-color 0.15s;
  }

  .display-description.editable:hover {
    background-color: var(--background-color);
  }

  .group-description {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .group-description.placeholder {
    font-style: italic;
    opacity: 0.6;
  }

  .edit-inline {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .form-control {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    transition: border-color 0.15s, box-shadow 0.15s;
    font-family: inherit;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px var(--primary-light);
  }

  .edit-name-input {
    font-size: 1.125rem;
    font-weight: 600;
  }

  .edit-desc-textarea {
    resize: vertical;
    min-height: 60px;
  }

  .edit-actions {
    display: flex;
    gap: 0.5rem;
  }

  .btn-inline-save,
  .btn-inline-cancel {
    padding: 0.25rem 0.75rem;
    border-radius: 6px;
    font-size: 0.75rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
  }

  .btn-inline-save {
    background: #3b82f6;
    color: white;
    border: none;
  }

  .btn-inline-save:hover:not(:disabled) {
    background: #2563eb;
  }

  .btn-inline-save:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-inline-cancel {
    background: transparent;
    color: var(--text-secondary);
    border: 1px solid var(--border-color);
  }

  .btn-inline-cancel:hover {
    background: var(--button-hover);
  }

  .detail-meta {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    font-size: 0.75rem;
    color: var(--text-secondary);
    flex-wrap: wrap;
  }

  .meta-separator {
    color: var(--border-color);
  }

  .section-divider {
    border-top: 1px solid var(--border-color);
    margin: 0.25rem 0;
  }

  .members-section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .members-title {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-color);
    margin: 0;
  }
</style>
