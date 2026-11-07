<!--
  FilePlaceHoldAction.svelte - "Place a legal hold" on the file page (legal-hold plan, Lane D).

  Rendered only for someone who may place a hold (an organization administrator in the managed
  edition, an instance administrator), never during a support session (the server refuses
  holds under a grant). Opens the shared PlaceHoldDialog for this one file. A file that is
  already held can take another hold, so the button stays available.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import PlaceHoldDialog from '$components/legal-hold/PlaceHoldDialog.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { holdAuthorityForCaller, type HoldPlacer } from '$lib/legalHold/authority';
  import { refreshHeldCount } from '$lib/legalHold/heldCount';
  import { isCloudEdition } from '$lib/edition';
  import { user as userStore } from '$stores/auth';
  import { capabilities } from '$stores/capabilities';
  import { t } from '$stores/locale';
  import { supportSessionGate } from '$stores/supportSession';

  export let fileUuid: string;
  export let fileLabel = '';

  const dispatch = createEventDispatcher<{ placed: { authority: HoldPlacer } }>();

  $: placer = holdAuthorityForCaller($capabilities, $userStore, isCloudEdition);
  let open = false;

  function placed() {
    if (placer) dispatch('placed', { authority: placer });
    refreshHeldCount(true);
  }
</script>

{#if placer && !$supportSessionGate.active}
  <button
    type="button"
    class="place-hold-btn"
    title={$t('holds.action.placeOnFile')}
    data-testid="file-place-hold"
    on:click={() => (open = true)}
  >
    <MenuIcon name="shield" size={14} />
    <span>{$t('holds.action.place')}</span>
  </button>
  <PlaceHoldDialog
    isOpen={open}
    {placer}
    fileUuids={[fileUuid]}
    {fileLabel}
    on:placed={placed}
    on:close={() => (open = false)}
  />
{/if}

<style>
  .place-hold-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
    padding: var(--space-1) var(--space-2-5);
    border: 1px solid var(--color-warning-border);
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--color-warning-text);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    box-shadow: none;
  }

  .place-hold-btn:hover {
    background: var(--color-warning-bg);
    box-shadow: none;
  }
</style>
