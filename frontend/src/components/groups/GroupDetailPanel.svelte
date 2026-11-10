<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { GroupDetail } from '$lib/types/groups';
  import { GroupsApi } from '$lib/api/groups';
  import { toastStore } from '$stores/toast';
  import { t, locale } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { formatDate } from '$lib/utils/formatting';
  import GroupRoleBadge from './GroupRoleBadge.svelte';
  import GroupMemberList from './GroupMemberList.svelte';
  import GroupMemberSearch from './GroupMemberSearch.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';

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
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    variant: confirmVariant,
  } = confirmDialog;

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
    confirmDialog.request(
      $t('groups.deleteGroup'),
      $t('groups.confirmDelete'),
      executeDelete,
      { confirmText: $t('groups.deleteGroup'), variant: 'danger' }
    );
  }

  async function executeDelete() {
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
    void refreshGroup();
  }

  function handleMemberRemoved() {
    void refreshGroup();
  }

  function handleRoleChanged() {
    void refreshGroup();
  }

  function handleLeft() {
    dispatch('left');
  }

  function handleNameKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      void saveName();
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
    <button class="btn btn-ghost btn-back" on:click={() => dispatch('back')}>
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="19" y1="12" x2="5" y2="12"></line>
        <polyline points="12 19 5 12 12 5"></polyline>
      </svg>
      <span>{$t('groups.backToGroups')}</span>
    </button>

    {#if canDelete}
      <button
        class="btn btn-danger-outline btn-sm btn-delete"
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
          <button class="btn btn-primary btn-sm btn-inline-save" on:click={saveName} disabled={isSaving}>
            {$t('common.save')}
          </button>
          <button class="btn btn-secondary btn-sm btn-inline-cancel" on:click={cancelEditName}>
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
          <button class="btn btn-primary btn-sm btn-inline-save" on:click={saveDescription} disabled={isSaving}>
            {$t('common.save')}
          </button>
          <button class="btn btn-secondary btn-sm btn-inline-cancel" on:click={cancelEditDescription}>
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
      {$t('groups.createdOn')} {formatDate(group.created_at, $locale)}
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
    bind:isOpen={$confirmOpen}
    title={$confirmTitle}
    message={$confirmMessage}
    confirmText={$confirmLabel}
    variant={$confirmVariant}
    on:confirm={confirmDialog.confirm}
    on:cancel={confirmDialog.cancel}
    on:close={confirmDialog.cancel}
  />
</div>

<style>
  .group-detail {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .detail-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--space-1);
  }

  .detail-name-section {
    margin-bottom: var(--space-0-5);
  }

  .display-name {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .display-name.editable {
    cursor: pointer;
    padding: var(--space-1) var(--space-2);
    margin: calc(-1 * var(--space-1)) calc(-1 * var(--space-2));
    border-radius: var(--radius-md);
    transition: background-color var(--duration-fast);
  }

  .display-name.editable:hover {
    background-color: var(--background-color);
  }

  .group-name {
    margin: 0;
    font-size: var(--font-size-xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .edit-icon {
    color: var(--text-secondary);
    opacity: 0;
    transition: opacity var(--duration-fast);
  }

  .display-name.editable:hover .edit-icon {
    opacity: 1;
  }

  .detail-description-section {
    min-height: 1.5rem;
  }

  .display-description.editable {
    cursor: pointer;
    padding: var(--space-1) var(--space-2);
    margin: calc(-1 * var(--space-1)) calc(-1 * var(--space-2));
    border-radius: var(--radius-md);
    transition: background-color var(--duration-fast);
  }

  .display-description.editable:hover {
    background-color: var(--background-color);
  }

  .group-description {
    margin: 0;
    font-size: var(--font-size-sm);
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
    gap: var(--space-2);
  }

  .form-control {
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-sm);
  }

  .edit-name-input {
    font-size: 1.125rem;
    font-weight: var(--font-weight-semibold);
  }

  .edit-desc-textarea {
    resize: vertical;
    min-height: 60px;
  }

  .edit-actions {
    display: flex;
    gap: var(--space-2);
  }

  .btn-back {
    color: var(--primary-on-surface);
  }

  .detail-meta {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    flex-wrap: wrap;
  }

  .meta-separator {
    color: var(--border-color);
  }

  .section-divider {
    border-top: 1px solid var(--border-color);
    margin: var(--space-1) 0;
  }

  .members-section {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .members-title {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin: 0;
  }
</style>
