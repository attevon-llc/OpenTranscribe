<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  const dispatch = createEventDispatcher<{ select: void }>();
</script>

<button class="dropdown-item" data-testid="extras-item" on:click={() => dispatch('select')}>
  <MenuIcon name="format-txt" size={16} />
  Extra entry
</button>
