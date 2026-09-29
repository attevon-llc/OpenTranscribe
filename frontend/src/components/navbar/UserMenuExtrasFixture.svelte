<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  const dispatch = createEventDispatcher<{ select: void }>();
</script>

<button class="dropdown-item" data-testid="extras-item" on:click={() => dispatch('select')}>
  Extra entry
</button>
