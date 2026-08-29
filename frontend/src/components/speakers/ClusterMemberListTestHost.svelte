<!--
  Test-only host — Svelte 5 removed `component.$on(...)`, so a legacy
  `createEventDispatcher` event is only observable through an `on:event`
  listener wired in a consuming component's markup. `ClustersTab.svelte` is
  that consumer in production; this host is that listener for
  ClusterMemberList.test.ts.
-->
<script lang="ts">
  import ClusterMemberList from './ClusterMemberList.svelte';
  import type { SpeakerCluster, SpeakerClusterMember } from '$lib/types/speakerCluster';

  export let members: SpeakerClusterMember[];
  export let cluster: SpeakerCluster;
  export let splitMode = false;
  export let splitTargetUuid: string | null = null;
  export let splitSelectedMembers: Set<string> = new Set();
  export let unassignMode = false;
  export let unassignTargetUuid: string | null = null;
  export let unassignSelectedMembers: Set<string> = new Set();
  export let onToggleSplitMember: (uuid: string) => void = () => {};
  export let onToggleUnassignMember: (uuid: string) => void = () => {};
</script>

<ClusterMemberList
  {members}
  {cluster}
  {splitMode}
  {splitTargetUuid}
  {splitSelectedMembers}
  {unassignMode}
  {unassignTargetUuid}
  {unassignSelectedMembers}
  on:toggleSplitMember={(e) => onToggleSplitMember(e.detail)}
  on:toggleUnassignMember={(e) => onToggleUnassignMember(e.detail)}
/>
