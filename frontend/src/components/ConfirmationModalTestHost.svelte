<script lang="ts">
  import ConfirmationModal from './ConfirmationModal.svelte';
  export let isOpen = false;
  export let variant: 'primary' | 'danger' | 'warning' = 'primary';
  export let onConfirm: () => void = () => {};
  export let onCancel: () => void = () => {};
</script>

<button type="button" data-testid="trigger" on:click={() => (isOpen = true)}>open</button>
<ConfirmationModal bind:isOpen {variant} on:confirm={onConfirm} on:cancel={onCancel} on:close={onCancel} />
