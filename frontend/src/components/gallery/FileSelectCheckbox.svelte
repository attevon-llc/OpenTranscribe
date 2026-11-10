<script lang="ts">
  /**
   * Selection checkbox for a gallery file, drawn as a custom checkmark. `card` floats in the
   * corner of a grid card; `row` fills the list table's checkbox column (width from the shared
   * `--file-list-check` track).
   */
  import { t } from '$stores/locale';
  import { handleCheckboxChange, handleCheckboxLabelClick } from '$lib/gallery/fileRowActions';

  export let fileId: string;
  export let checked: boolean;
  export let variant: 'card' | 'row' = 'card';
</script>

<label
  role="presentation"
  class="file-selector {variant}"
  on:click|stopPropagation={(e) => handleCheckboxLabelClick(fileId, e)}
>
  <input
    type="checkbox"
    class="file-checkbox"
    {checked}
    on:change={(e) => handleCheckboxChange(fileId, e)}
    title={$t('gallery.selectFileTooltip')}
  />
  <span class="checkmark"></span>
</label>

<style>
  .file-selector {
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .file-selector.card {
    position: absolute;
    top: 6px;
    inset-inline-end: 6px;
    z-index: var(--z-sticky);
  }

  .file-selector.row {
    width: var(--file-list-check);
    min-width: 0;
    position: relative;
  }

  .checkmark {
    width: 18px;
    height: 18px;
    background-color: var(--background-color);
    border: 2px solid var(--border-color);
    border-radius: var(--radius-sm);
    position: relative;
    cursor: pointer;
    transition: all var(--duration-base) ease;
  }

  .file-selector:hover .checkmark {
    border-color: var(--info-color);
  }

  .file-checkbox:checked ~ .checkmark {
    background-color: var(--primary-color);
    border-color: var(--info-color);
  }

  .file-checkbox {
    position: absolute;
    opacity: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    cursor: pointer;
  }

  .checkmark:after {
    content: '';
    position: absolute;
    display: none;
    inset-inline-start: 5px;
    top: 1px;
    width: 5px;
    height: 10px;
    border: solid var(--text-on-solid);
    border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }

  .file-checkbox:checked ~ .checkmark:after {
    display: block;
  }

  @media (max-width: 768px) {
    .file-selector.card {
      top: 4px;
      inset-inline-end: 4px;
    }
  }
</style>
