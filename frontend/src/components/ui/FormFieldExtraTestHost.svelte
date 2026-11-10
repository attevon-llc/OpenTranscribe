<script lang="ts">
  import FormField from './FormField.svelte';
  export let withExtra = true;
</script>

<FormField id="g" label="Groups" hint="First hint" class="narrow" let:describedby>
  <input id="g" aria-describedby={describedby} />
  <svelte:fragment slot="after-hint">
    {#if withExtra}<p>Second line</p>{/if}
  </svelte:fragment>
</FormField>
