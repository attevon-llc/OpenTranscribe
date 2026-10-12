<script lang="ts">
  import InfoTip from './InfoTip.svelte';

  export let title: string;
  export let description = '';
  export let tooltip = '';
</script>

<div class="settings-section">
  <div class="title-row">
    <h3 class="section-title">{title}</h3>
    {#if tooltip}
      <InfoTip>{tooltip}</InfoTip>
    {/if}
  </div>
  {#if description}
    <p class="section-desc">{description}</p>
  {/if}
  <slot />
</div>
