<script lang="ts">
  import FormField from './FormField.svelte';
</script>

<FormField id="f" label="Host">
  <button slot="label-after" type="button">toggle</button>
  <input id="f" />
</FormField>
