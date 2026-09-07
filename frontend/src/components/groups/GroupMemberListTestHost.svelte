<!--
  Test-only host — Svelte 5 removed `component.$on(...)`, so a legacy
  `createEventDispatcher` event is only observable through an `on:event`
  listener wired in a consuming component's markup. `GroupDetailPanel.svelte`
  is that consumer in production; this host is that listener for
  GroupMemberList.test.ts.
-->
<script lang="ts">
  import GroupMemberList from './GroupMemberList.svelte';
  import type { GroupMember, GroupRole } from '$lib/types/groups';

  export let members: GroupMember[] = [];
  export let groupUuid: string;
  export let myRole: GroupRole;
  export let onMemberRemoved: (detail: { userUuid: string }) => void = () => {};
  export let onRoleChanged: (detail: { userUuid: string; newRole: GroupRole }) => void = () => {};
  export let onLeft: () => void = () => {};
</script>

<GroupMemberList
  {members}
  {groupUuid}
  {myRole}
  on:memberRemoved={(e) => onMemberRemoved(e.detail)}
  on:roleChanged={(e) => onRoleChanged(e.detail)}
  on:left={onLeft}
/>
