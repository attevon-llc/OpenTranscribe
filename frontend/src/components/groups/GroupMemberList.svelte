<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { GroupMember, GroupRole } from '$lib/types/groups';
  import { GroupsApi } from '$lib/api/groups';
  import { toastStore } from '$stores/toast';
  import { authStore } from '$stores/auth';
  import { t, locale } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { formatDate, getInitials } from '$lib/utils/formatting';
  import GroupRoleBadge from './GroupRoleBadge.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';

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
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    variant: confirmVariant,
  } = confirmDialog;

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
    const leavingSelf = member.user_uuid === currentUserUuid;
    confirmDialog.request(
      leavingSelf ? $t('groups.leaveGroup') : $t('groups.removeMember'),
      leavingSelf ? $t('groups.confirmLeave') : $t('groups.confirmRemoveMember'),
      () => executeRemoveMember(member),
      {
        confirmText: leavingSelf ? $t('groups.leaveGroup') : $t('groups.removeMember'),
        variant: 'danger',
      }
    );
  }

  async function executeRemoveMember(member: GroupMember) {
    const isLeavingSelf = member.user_uuid === currentUserUuid;

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
          <span class="member-joined">{$t('groups.joined')} {formatDate(member.joined_at, $locale)}</span>
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
              class="btn btn-icon btn-sm btn-icon-danger btn-remove"
              on:click={() => requestRemoveMember(member)}
              disabled={removingMember === member.user_uuid}
              title={$t('groups.removeMember')}
              aria-label={$t('groups.removeMember')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <line x1="18" y1="6" x2="6" y2="18"></line>
                <line x1="6" y1="6" x2="18" y2="18"></line>
              </svg>
            </button>
          {:else if member.user_uuid === currentUserUuid && member.role !== 'owner'}
            <button
              class="btn btn-danger-outline btn-sm btn-leave"
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
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$confirmLabel}
  variant={$confirmVariant}
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>
  .member-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .empty-state {
    text-align: center;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    padding: var(--space-6) 0;
    margin: 0;
  }

  .member-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2-5) var(--space-3);
    border-radius: var(--radius-lg);
    transition: background-color var(--duration-fast);
  }

  .member-row:hover {
    background-color: var(--background-color);
  }

  .member-row.is-self {
    background-color: var(--primary-light);
  }

  .member-avatar {
    width: 36px;
    height: 36px;
    border-radius: var(--radius-round);
    background-color: var(--primary-color);
    color: var(--text-on-solid);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-semibold);
    flex-shrink: 0;
  }

  .member-info {
    flex: 1;
    min-width: 0;
  }

  .member-name {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .you-label {
    font-weight: var(--font-weight-regular);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .member-email {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .member-meta {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: var(--space-1);
    flex-shrink: 0;
  }

  .member-joined {
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .member-actions {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-shrink: 0;
  }

  .role-select {
    padding: var(--space-1) var(--space-2);
    font-size: var(--font-size-xs);
    cursor: pointer;
  }

  .role-select:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

</style>
