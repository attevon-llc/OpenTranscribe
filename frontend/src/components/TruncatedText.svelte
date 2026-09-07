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
    margin: 0 0 0.25rem 0;
    white-space: pre-wrap;
    word-wrap: break-word;
    line-height: 1.5;
  }

  .toggle-truncation {
    background: none;
    border: none;
    color: var(--primary-on-surface);
    padding: 0;
    margin: 0;
    font-size: 0.85rem;
    cursor: pointer;
    text-decoration: underline;
    font-weight: 500;
  }

  .toggle-truncation:hover {
    color: var(--primary-color-dark);
  }
</style>
