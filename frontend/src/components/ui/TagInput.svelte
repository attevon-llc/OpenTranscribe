<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  export let tags: string[] = [];
  export let placeholder: string | undefined = undefined;

  $: resolvedPlaceholder = placeholder ?? $t('common.addTagPlaceholder');
  export let disabled = false;
  export let maxTags = 20;
  export let maxLength = 50;
  export let id = 'tag-input';

  const dispatch = createEventDispatcher();

  let inputValue = '';

  function addTag() {
    const tag = inputValue.trim().toLowerCase();
    if (!tag || tag.length > maxLength) return;
    if (tags.length >= maxTags) return;
    if (!isValidTag(tag)) return;
    if (tags.includes(tag)) {
      inputValue = '';
      return;
    }

    tags = [...tags, tag];
    inputValue = '';
    dispatch('change', tags);
  }

  function removeTag(index: number) {
    tags = tags.filter((_, i) => i !== index);
    dispatch('change', tags);
  }

  function isValidTag(tag: string): boolean {
    return tag.split('').every(c =>
      (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c === '-' || c === '_' || c === ' '
    );
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      addTag();
    } else if (event.key === 'Backspace' && inputValue === '' && tags.length > 0) {
      removeTag(tags.length - 1);
    }
  }
</script>

<div class="tag-input-container" class:disabled>
  <div class="tags-wrapper">
    {#each tags as tag, i}
      <span class="tag-pill">
        {tag}
        {#if !disabled}
          <button
            type="button"
            class="tag-remove"
            on:click={() => removeTag(i)}
            aria-label="{$t('tags.removeTag')} {tag}"
          >&times;</button>
        {/if}
      </span>
    {/each}
    {#if !disabled && tags.length < maxTags}
      <input
        type="text"
        class="tag-input"
        {id}
        bind:value={inputValue}
        on:keydown={handleKeydown}
        on:blur={addTag}
        placeholder={resolvedPlaceholder}
        maxlength={maxLength}
      />
    {/if}
  </div>
</div>

<style>
  .tag-input-container {
    display: flex;
    flex-wrap: wrap;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    padding: 0.375rem;
    background: var(--background-color);
    min-height: 38px;
    transition: border-color 0.2s ease;
    cursor: text;
  }

  .tag-input-container:focus-within {
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
  }

  .tag-input-container.disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .tags-wrapper {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    align-items: center;
    width: 100%;
  }

  .tag-pill {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.6875rem;
    font-weight: 500;
    background: rgba(59, 130, 246, 0.12);
    color: var(--primary-on-surface);
    gap: 0.25rem;
    white-space: nowrap;
  }

  :global([data-theme='dark']) .tag-pill,
  :global([data-theme='dark']) .tag-pill {
    background: rgba(59, 130, 246, 0.2);
    color: #60a5fa;
  }

  .tag-remove {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    color: inherit;
    cursor: pointer;
    padding: 0;
    font-size: 0.875rem;
    line-height: 1;
    opacity: 0.7;
    transition: opacity 0.15s;
  }

  .tag-remove:hover {
    opacity: 1;
  }

  .tag-input {
    flex: 1;
    min-width: 80px;
    border: none;
    outline: none;
    background: transparent;
    font-size: 0.8125rem;
    color: var(--text-color);
    padding: 2px 4px;
  }

  .tag-input::placeholder {
    color: var(--text-muted);
    opacity: 0.6;
  }
</style>
