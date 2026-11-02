<script lang="ts">
  import SummaryModal from './SummaryModal.svelte';
  export let onClose: () => void = () => {};
</script>

<SummaryModal isOpen={true} fileId="file-uuid" fileName="meeting.mp4" on:close={onClose} />
