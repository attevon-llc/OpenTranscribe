<script lang="ts">
  import { t } from '$stores/locale';
  import type { UserBrief } from '$lib/types/groups';

  export let sharedBy: UserBrief;

  $: displayName = sharedBy.full_name || sharedBy.email;
</script>

<span class="shared-by" title={sharedBy.email}>
  {$t('sharing.sharedBy', { name: displayName })}
</span>

<style>
  .shared-by {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    font-style: italic;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
