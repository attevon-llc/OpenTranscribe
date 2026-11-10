<script lang="ts">
  import type { GroupRole } from '$lib/types/groups';
  import { t } from '$stores/locale';

  export let role: GroupRole;

  $: label = {
    owner: $t('groups.roles.owner'),
    admin: $t('groups.roles.admin'),
    member: $t('groups.roles.member'),
  }[role];
</script>

<span class="role-badge" class:role-owner={role === 'owner'} class:role-admin={role === 'admin'} class:role-member={role === 'member'}>
  {label}
</span>

<style>
  .role-badge {
    display: inline-flex;
    align-items: center;
    padding: var(--space-0-5) var(--space-2);
    border-radius: 9999px;
    font-size: 0.75rem;
    font-weight: 500;
    line-height: 1.25rem;
    white-space: nowrap;
  }

  .role-owner {
    background-color: rgba(251, 191, 36, 0.15);
    color: #92400e;
  }

  .role-admin {
    background-color: rgba(var(--accent-rgb), 0.12);
    color: #1e40af;
  }

  .role-member {
    background-color: var(--background-color);
    color: var(--text-secondary);
  }

  :global([data-theme='dark']) .role-owner {
    background-color: rgba(251, 191, 36, 0.15);
    color: #fcd34d;
  }

  :global([data-theme='dark']) .role-admin {
    background-color: rgba(var(--accent-rgb), 0.15);
    color: #93c5fd;
  }

  :global([data-theme='dark']) .role-member {
    background-color: rgba(107, 114, 128, 0.2);
    color: #d1d5db;
  }
</style>
