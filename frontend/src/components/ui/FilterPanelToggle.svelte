<script lang="ts">
  /**
   * The compact chevron that collapses/expands a left filter sidebar. Shared by
   * the gallery and Transcript Search so the two can never drift apart again
   * (issue #1197). One glyph and one `rotate()` keeps it RTL-correct.
   */
  import { t } from '$stores/locale';

  export let expanded: boolean;

  $: label = expanded ? $t('gallery.hideFiltersPanel') : $t('gallery.showFiltersPanel');
</script>

<button
  type="button"
  class="filter-toggle-btn {expanded ? 'expanded' : 'collapsed'}"
  aria-expanded={expanded}
  title={label}
  aria-label={label}
  on:click
>
  <svg
    xmlns="http://www.w3.org/2000/svg"
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    class="toggle-chevron"
    class:rotated={expanded}
    aria-hidden="true"
  >
    <polyline points="9 18 15 12 9 6"></polyline>
  </svg>
</button>

<style>
  .filter-toggle-btn {
    width: 32px;
    height: 32px;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
  }

  .filter-toggle-btn:hover {
    background-color: var(--hover-color);
    border-color: var(--primary-color);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  }

  .filter-toggle-btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .filter-toggle-btn:active {
    transform: scale(0.94);
  }

  .toggle-chevron {
    flex-shrink: 0;
    opacity: 0.8;
    transition: transform 0.2s ease;
  }

  .toggle-chevron.rotated {
    transform: rotate(180deg);
  }
</style>
