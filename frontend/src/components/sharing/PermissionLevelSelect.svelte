<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { PermissionLevel } from '$lib/types/groups';

  export let value: PermissionLevel = 'viewer';
  export let disabled: boolean = false;

  const dispatch = createEventDispatcher();

  const options: { value: PermissionLevel; labelKey: string; descKey: string }[] = [
    { value: 'viewer', labelKey: 'sharing.permissionViewer', descKey: 'sharing.permissionViewerDesc' },
    { value: 'editor', labelKey: 'sharing.permissionEditor', descKey: 'sharing.permissionEditorDesc' },
  ];

  function handleChange(event: Event) {
    const target = event.target as HTMLSelectElement;
    const newValue = target.value as PermissionLevel;
    value = newValue;
    dispatch('change', newValue);
  }
</script>

<select
  class="permission-select"
  {disabled}
  {value}
  on:change={handleChange}
  title={$t(options.find(o => o.value === value)?.descKey || '')}
>
  {#each options as option}
    <option value={option.value} title={$t(option.descKey)}>
      {$t(option.labelKey)}
    </option>
  {/each}
</select>

<style>
  .permission-select {
    padding: 0.35rem 0.5rem;
    font-size: 0.8rem;
    cursor: pointer;
    min-width: 90px;
  }

  .permission-select:hover:not(:disabled) {
    border-color: var(--primary-color);
  }

  .permission-select:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
