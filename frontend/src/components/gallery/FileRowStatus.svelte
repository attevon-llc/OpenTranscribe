<script lang="ts">
  /**
   * The status affordance of one gallery file, shared by the grid card and the list row.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import FileStatusIndicator from './FileStatusIndicator.svelte';
  import type { MediaFile } from '$lib/types/media';

  export let file: MediaFile;
  export let showLabel = false;

  const dispatch = createEventDispatcher<{ errorclick: MediaFile }>();

  $: hasErrorDetails = file.status === 'error' && !!file.user_message;
</script>

<!-- Keyed off `is_quarantined`, never bare `status` (issue #576 E11): a file quarantined
     mid-pipeline can have `status` overwritten to a terminal value by a running task while
     this flag stays correct. -->
<FileStatusIndicator
  status={file.is_quarantined ? 'quarantined' : file.status}
  displayStatus={hasErrorDetails ? $t('gallery.errorClickForDetails') : undefined}
  clickable={hasErrorDetails}
  {showLabel}
  on:click={() => dispatch('errorclick', file)}
/>
