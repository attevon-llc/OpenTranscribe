<script lang="ts">
  import ExportOptionsModal from './ExportOptionsModal.svelte';
  import { t } from '$stores/locale';

  // Visibility (two-way bound by the page; the page owns the scroll-lock sequencing)
  export let show = false;

  // TXT export option values (two-way bound to the page's txtExportOptions object)
  export let includeTimestamps = true;
  export let includeSpeakers = true;
  export let includeComments = false;
  export let hasComments = false;

  // Read-only display context
  export let diarizationDisabled = false;
  export let redactionActive = false;
  export let showOriginal = false;
  export let canViewOriginal = false;
</script>

<ExportOptionsModal
  title={$t('exportOptions.title')}
  description={$t('exportOptions.description')}
  bind:show
  bind:includeTimestamps
  bind:includeSpeakers
  bind:includeComments
  {hasComments}
  {diarizationDisabled}
  {redactionActive}
  {showOriginal}
  {canViewOriginal}
  on:close
  on:confirm
/>
