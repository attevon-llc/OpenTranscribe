<script>
  import { t } from '$stores/locale';

  export let text = "";
  export let maxLength = 150;
  export let showMoreText = "";
  export let showLessText = "";

  let expanded = false;

  $: resolvedShowMoreText = showMoreText || $t('common.seeMore');
  $: resolvedShowLessText = showLessText || $t('common.seeLess');

  $: needsTruncation = text.length > maxLength;
  $: displayText = needsTruncation && !expanded
    ? text.substring(0, maxLength).trim() + '...'
    : text;

  function toggleExpanded() {
    expanded = !expanded;
  }
</script>

<div class="truncated-text">
  <p>{displayText}</p>
  {#if needsTruncation}
    <button
      type="button"
      class="toggle-truncation"
      on:click={toggleExpanded}
      title={expanded ? $t('common.showLessTooltip') : $t('common.showMoreTooltip')}
    >
      {expanded ? resolvedShowLessText : resolvedShowMoreText}
    </button>
  {/if}
</div>

<style>
  .truncated-text {
    position: relative;
    width: 100%;
  }

  .truncated-text p {
    margin: 0 0 var(--space-1) 0;
    white-space: pre-wrap;
    overflow-wrap: break-word;
    line-height: 1.5;
  }

  .toggle-truncation {
    background: none;
    border: none;
    color: var(--primary-on-surface);
    padding: 0;
    margin: 0;
    font-size: var(--font-size-md);
    cursor: pointer;
    text-decoration: underline;
    font-weight: var(--font-weight-medium);
  }

  .toggle-truncation:hover {
    color: var(--primary-color-dark);
  }
</style>
