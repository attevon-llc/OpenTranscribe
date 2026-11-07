<!--
  GalleryLegalHold.svelte - "Place legal hold" on the gallery selection (legal-hold plan, Lane D).

  Listens for the Organize menu's trigger, opens the shared PlaceHoldDialog over the selected
  files with the caller's authority, and reports which files were held so the page can show
  the badge on those rows without a reload. Renders nothing for a caller who may not place a
  hold (the menu entry is not offered to them either).
-->
<script lang="ts">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import PlaceHoldDialog from '$components/legal-hold/PlaceHoldDialog.svelte';
  import { holdAuthorityForCaller, type HoldPlacer } from '$lib/legalHold/authority';
  import { refreshHeldCount } from '$lib/legalHold/heldCount';
  import { isCloudEdition } from '$lib/edition';
  import type { MediaFile } from '$lib/types/media';
  import { user as userStore } from '$stores/auth';
  import { capabilities } from '$stores/capabilities';
  import { galleryState, galleryStore } from '$stores/gallery';

  /** The rows the page has loaded; the selection is read from the gallery store. */
  export let files: MediaFile[] = [];

  const dispatch = createEventDispatcher<{
    placed: { uuids: string[]; authority: HoldPlacer };
  }>();

  $: placer = holdAuthorityForCaller($capabilities, $userStore, isCloudEdition);

  let open = false;
  let targets: string[] = [];

  const unsubscribe = galleryStore.onPlaceHoldTrigger(() => {
    targets = Array.from($galleryState.selectedFiles);
    open = placer !== null && targets.length > 0;
  });
  onDestroy(unsubscribe);

  $: singleLabel =
    targets.length === 1
      ? (files.find((f) => f.uuid === targets[0])?.title ??
        files.find((f) => f.uuid === targets[0])?.filename ??
        '')
      : '';

  function placed(event: CustomEvent<{ fileUuids: string[] }>) {
    if (placer) dispatch('placed', { uuids: event.detail.fileUuids, authority: placer });
    refreshHeldCount(true);
  }
</script>

{#if placer}
  <PlaceHoldDialog
    isOpen={open}
    {placer}
    fileUuids={targets}
    fileLabel={singleLabel}
    on:placed={placed}
    on:close={() => (open = false)}
  />
{/if}
