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
    border-radius: var(--radius-pill);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    line-height: 1.25rem;
    white-space: nowrap;
  }

  .role-owner {
    background-color: rgba(var(--warning-color-rgb), 0.15);
    color: var(--warning-text);
  }

  .role-admin {
    background-color: rgba(var(--accent-rgb), 0.12);
    color: var(--primary-on-surface);
  }

  .role-member {
    background-color: var(--background-color);
    color: var(--text-secondary);
  }
</style>
