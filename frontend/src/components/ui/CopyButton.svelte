<script lang="ts">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { copyToClipboard } from '$lib/utils/clipboard';
  import { t } from '$stores/locale';

  /** Text that gets written to the clipboard. */
  export let text: string;
  /** Button label in the idle state. Defaults to the translated "Copy". */
  export let label: string | undefined = undefined;
  /** Label shown briefly after a successful copy. Defaults to the translated "Copied". */
  export let copiedLabel: string | undefined = undefined;

  $: idleLabel = label ?? $t('common.copy');
  $: doneLabel = copiedLabel ?? $t('common.copied');
  /** When true, only the icon shows; the label becomes the aria-label. */
  export let iconOnly = false;
  /** Optional native tooltip text. */
  export let title: string | undefined = undefined;

  const dispatch = createEventDispatcher<{ copied: void }>();
  const COPIED_MS = 1500;

  let copied = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  async function onClick() {
    await copyToClipboard(
      text,
      () => {
        copied = true;
        dispatch('copied');
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => {
          copied = false;
          timer = null;
        }, COPIED_MS);
      },
      () => {
        copied = false;
      }
    );
  }

  onDestroy(() => {
    if (timer) clearTimeout(timer);
  });

  $: currentLabel = copied ? doneLabel : idleLabel;
</script>

<button
  type="button"
  class="copy-button"
  class:icon-only={iconOnly}
  class:copied
  aria-label={iconOnly ? currentLabel : undefined}
  {title}
  on:click={onClick}
>
  {#if copied}
    <svg
      class="copy-icon"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  {:else}
    <svg
      class="copy-icon"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  {/if}
  {#if !iconOnly}
    <span class="copy-label">{currentLabel}</span>
  {/if}
</button>

<style>
  .copy-button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-color);
    font-size: 13px;
    font-weight: 500;
    line-height: 1;
    cursor: pointer;
    transition:
      background 0.15s ease,
      border-color 0.15s ease,
      color 0.15s ease;
  }
  .copy-button.icon-only {
    padding: 6px;
  }
  .copy-button:hover {
    background: var(--button-hover);
    color: var(--text-color);
  }
  .copy-button.copied {
    color: var(--success-color, #16a34a);
    border-color: var(--success-color, #16a34a);
  }
  .copy-button:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
  .copy-icon {
    flex-shrink: 0;
  }
</style>
