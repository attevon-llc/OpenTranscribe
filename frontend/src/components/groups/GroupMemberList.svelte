<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { GroupMember, GroupRole } from '$lib/types/groups';
  import { GroupsApi } from '$lib/api/groups';
  import { toastStore } from '$stores/toast';
  import { authStore } from '$stores/auth';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { formatDate, getInitials } from '$lib/utils/formatting';
  import GroupRoleBadge from './GroupRoleBadge.svelte';
  import ConfirmationModal from '../ConfirmationModal.svelte';

  export let members: GroupMember[] = [];
  export let groupUuid: string;
  export let myRole: GroupRole;

  const dispatch = createEventDispatcher<{
    memberRemoved: { userUuid: string };
    roleChanged: { userUuid: string; newRole: GroupRole };
    left: void;
  }>();

  $: canManageMembers = myRole === 'owner' || myRole === 'admin';
  $: currentUserUuid = $authStore.user?.uuid;

  let changingRoleFor: string | null = null;
  let removingMember: string | null = null;
  let showRemoveConfirm = false;
  let pendingRemoveMember: GroupMember | null = null;

  $: pendingIsLeavingSelf = pendingRemoveMember?.user_uuid === currentUserUuid;
  $: removeConfirmMessage = pendingIsLeavingSelf ? $t('groups.confirmLeave') : $t('groups.confirmRemoveMember');
  $: removeConfirmTitle = pendingIsLeavingSelf ? $t('groups.leaveGroup') : $t('groups.removeMember');

  async function handleRoleChange(member: GroupMember, newRole: 'admin' | 'member') {
    if (newRole === member.role) return;
    changingRoleFor = member.user_uuid;

    try {
      await GroupsApi.updateMemberRole(groupUuid, member.user_uuid, { role: newRole });
      toastStore.success($t('groups.toast.roleUpdated'));
      dispatch('roleChanged', { userUuid: member.user_uuid, newRole });
    } catch (err: unknown) {
      const message = getErrorMessage(err, $t('groups.toast.roleUpdateFailed'));
      toastStore.error(message);
    } finally {
      changingRoleFor = null;
    }
  }

  function requestRemoveMember(member: GroupMember) {
    pendingRemoveMember = member;
    showRemoveConfirm = true;
  }

  async function executeRemoveMember() {
    if (!pendingRemoveMember) return;
    const member = pendingRemoveMember;
    const isLeavingSelf = member.user_uuid === currentUserUuid;
    showRemoveConfirm = false;
    pendingRemoveMember = null;

    removingMember = member.user_uuid;

    try {
      await GroupsApi.removeMember(groupUuid, member.user_uuid);

      if (isLeavingSelf) {
        toastStore.success($t('groups.toast.leftGroup'));
        dispatch('left');
      } else {
        toastStore.success($t('groups.toast.memberRemoved'));
        dispatch('memberRemoved', { userUuid: member.user_uuid });
      }
    } catch (err: unknown) {
      const message = getErrorMessage(err, $t('groups.toast.removeMemberFailed'));
      toastStore.error(message);
    } finally {
      removingMember = null;
    }
  }

</script>

<div class="member-list">
  {#if members.length === 0}
    <p class="empty-state">{$t('groups.noMembers')}</p>
  {:else}
    {#each members as member (member.uuid)}
      <div class="member-row" class:is-self={member.user_uuid === currentUserUuid}>
        <div class="member-avatar">
          {getInitials(member.full_name, member.email)}
        </div>

        <div class="member-info">
          <div class="member-name">
            {member.full_name || member.email}
            {#if member.user_uuid === currentUserUuid}
              <span class="you-label">({$t('groups.you')})</span>
            {/if}
          </div>
          <div class="member-email">{member.email}</div>
        </div>

        <div class="member-meta">
          <GroupRoleBadge role={member.role} />
          <span class="member-joined">{$t('groups.joined')} {formatDate(member.joined_at)}</span>
        </div>

        <div class="member-actions">
          {#if canManageMembers && member.role !== 'owner'}
            <select
              class="role-select"
              value={member.role}
              on:change={(e) => handleRoleChange(member, e.currentTarget.value as 'admin' | 'member')}
              disabled={changingRoleFor === member.user_uuid}
            >
              <option value="admin">{$t('groups.roles.admin')}</option>
              <option value="member">{$t('groups.roles.member')}</option>
            </select>

            <button
              class="btn-remove"
              on:click={() => requestRemoveMember(member)}
              disabled={removingMember === member.user_uuid}
              title={$t('groups.removeMember')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <line x1="18" y1="6" x2="6" y2="18"></line>
                <line x1="6" y1="6" x2="18" y2="18"></line>
              </svg>
            </button>
          {:else if member.user_uuid === currentUserUuid && member.role !== 'owner'}
            <button
              class="btn-leave"
              on:click={() => requestRemoveMember(member)}
              disabled={removingMember === member.user_uuid}
            >
              {$t('groups.leaveGroup')}
            </button>
          {/if}
        </div>
      </div>
    {/each}
  {/if}
</div>

<ConfirmationModal
  bind:isOpen={showRemoveConfirm}
  title={removeConfirmTitle}
  message={removeConfirmMessage}
  confirmText={pendingIsLeavingSelf ? $t('groups.leaveGroup') : $t('groups.removeMember')}
  cancelText={$t('modal.cancel')}
  confirmButtonClass="modal-delete-button"
  on:confirm={executeRemoveMember}
  on:cancel={() => { showRemoveConfirm = false; pendingRemoveMember = null; }}
  on:close={() => { showRemoveConfirm = false; pendingRemoveMember = null; }}
/>

<style>
  .member-list {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .empty-state {
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.8125rem;
    padding: 1.5rem 0;
    margin: 0;
  }

  .member-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.625rem 0.75rem;
    border-radius: 8px;
    transition: background-color 0.15s;
  }

  .member-row:hover {
    background-color: var(--background-color);
  }

  .member-row.is-self {
    background-color: var(--primary-light, rgba(59, 130, 246, 0.05));
  }

  .member-avatar {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background-color: #3b82f6;
    color: white;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.75rem;
    font-weight: 600;
    flex-shrink: 0;
  }

  .member-info {
    flex: 1;
    min-width: 0;
  }

  .member-name {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .you-label {
    font-weight: 400;
    color: var(--text-secondary);
    font-size: 0.75rem;
  }

  .member-email {
    font-size: 0.75rem;
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .member-meta {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 0.25rem;
    flex-shrink: 0;
  }

  .member-joined {
    font-size: 0.6875rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .member-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .role-select {
    padding: 0.25rem 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.75rem;
    cursor: pointer;
    transition: border-color 0.15s;
  }

  .role-select:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px var(--primary-light);
  }

  .role-select:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-remove {
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.25rem;
    color: var(--text-secondary);
    border-radius: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s;
  }

  .btn-remove:hover:not(:disabled) {
    color: var(--danger-color, #dc2626);
    background-color: rgba(220, 38, 38, 0.08);
  }

  .btn-remove:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .btn-leave {
    padding: 0.25rem 0.75rem;
    border: 1px solid var(--danger-color, #dc2626);
    border-radius: 6px;
    background: transparent;
    color: var(--danger-color, #dc2626);
    font-size: 0.75rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
  }

  .btn-leave:hover:not(:disabled) {
    background-color: rgba(220, 38, 38, 0.08);
  }

  .btn-leave:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
