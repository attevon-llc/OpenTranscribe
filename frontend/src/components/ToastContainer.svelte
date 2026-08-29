<script lang="ts">
  import { toastStore } from '$stores/toast';
  import Toast from './Toast.svelte';
</script>

{#if $toastStore.length > 0}
  <div class="toast-container">
    {#each $toastStore as toast (toast.id)}
      <Toast
        message={toast.message}
        type={toast.type}
        duration={0}
        on:dismiss={() => toastStore.dismiss(toast.id)}
      />
    {/each}
  </div>
{/if}

<style>
  .toast-container {
    position: fixed;
    bottom: calc(20px + env(safe-area-inset-bottom, 0px));
    left: 50%;
    transform: translateX(-50%);
    z-index: var(--z-toast);
    display: flex;
    flex-direction: column;
    gap: 8px;
    align-items: center;
    pointer-events: none;
  }

  .toast-container :global(.toast) {
    pointer-events: auto;
  }

  @media (max-width: 640px) {
    .toast-container {
      left: 20px;
      right: 20px;
      transform: none;
      width: auto;
    }
  }
</style>
