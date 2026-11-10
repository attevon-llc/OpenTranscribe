<script lang="ts">
  import FormField from './FormField.svelte';

  export let id: string;
  export let label: string;
  export let hint = '';
  export let error = '';
  export let required = false;
  export let size: 'md' | 'lg' = 'md';
</script>

<FormField {id} {label} {hint} {error} {required} {size} let:describedby let:invalid>
  <input {id} aria-describedby={describedby} aria-invalid={invalid} />
</FormField>
