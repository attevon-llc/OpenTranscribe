<script lang="ts">
  import ConfirmationModal from './ConfirmationModal.svelte';
  export let isOpen = false;
  export let onConfirm: () => void = () => {};
</script>

<ConfirmationModal {isOpen} on:confirm={onConfirm} />
