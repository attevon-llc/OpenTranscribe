<!--
  HoldBadge.svelte — the "Legal hold" pill on a file or collection (issue #1276).

  Renders nothing unless the server says the item is held. It states who holds it (the hover
  text) and never why: a hold's reason is for the holding organization's administrators only.
-->
<script lang="ts">
  import Badge from '$components/ui/Badge.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import type { HoldSummary } from '$lib/types/hold';
  import { HOLD_BADGE_TITLE_KEYS, holdAuthorityKey, labelFor } from '$lib/i18n/keyMaps';
  import { t } from '$stores/locale';

  export let hold: HoldSummary | null | undefined = undefined;

  $: title = labelFor($t, HOLD_BADGE_TITLE_KEYS, holdAuthorityKey(hold?.held_by));
</script>

{#if hold?.held}
  <Badge variant="warning" title={title || undefined}>
    <MenuIcon name="shield" size={12} />
    {$t('holds.badge')}
  </Badge>
{/if}
