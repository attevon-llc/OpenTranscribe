<!--
  BaseModal.svelte — Shared modal wrapper component.

  Extracts common modal chrome (backdrop, container, header, close button,
  body, footer) that was duplicated across 6+ modal components.

  Usage:
    <BaseModal {isOpen} title="My Modal" onClose={handleClose}>
      <p>Modal body content</p>
      <svelte:fragment slot="footer">
        <button on:click={handleClose}>Cancel</button>
        <button on:click={handleSave}>Save</button>
      </svelte:fragment>
    </BaseModal>
-->
<script context="module" lang="ts">
  /**
   * Open dialogs, oldest first. Only the last one answers Escape: every instance listens on
   * `window`, so without this one key press closed a dialog AND the one it was opened from
   * (the legal reader over the first-run wizard).
   */
  const openStack: symbol[] = [];
</script>

<script lang="ts">
  import { t } from '$stores/locale';
  import { onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { focusTrap } from '$lib/actions/focusTrap';

  export let isOpen = false;
  export let title = '';
  /**
   * Dialog width on one shared scale, so modals line up instead of each picking its own
   * pixel value (there were 18 distinct ones): sm 480 (confirmations, short forms),
   * md 600 (the default form), lg 720 (forms with two columns or a table), xl 960 (editors,
   * pickers), 2xl 1200 (full workspaces). Below 768px every size is full screen.
   */
  export let size: 'sm' | 'md' | 'lg' | 'xl' | '2xl' = 'md';
  const WIDTHS = { sm: '480px', md: '600px', lg: '720px', xl: '960px', '2xl': '1200px' } as const;
  $: maxWidth = WIDTHS[size] ?? WIDTHS.md;
  /**
   * Optional fixed dialog height, e.g. `min(90vh, 780px)`.
   *
   * Applied INLINE, like `maxWidth` — deliberately not left to the call site's
   * stylesheet. A caller that pinned this with
   * `.host :global(.modal-container) { height: … }` had the rule silently stop
   * applying: the host's only child is this component, so the descendant half
   * of that selector crosses a component boundary, and svelte-check reports no
   * unused-selector warning to say so. Empty means content-sized, which is what
   * every smaller consumer (confirmations, pickers) wants.
   */
  export let height = '';
  /**
   * Fill the whole viewport (a document reader). The header and footer stay put and only the
   * body scrolls, as in the sized dialog; the width cap from `size` is dropped.
   */
  export let fullscreen = false;
  /** CSS z-index value. Defaults to the shared `--z-modal` tier (H5) — pass a
   *  higher value (e.g. a toast/critical tier) only for a modal that must
   *  layer above another modal it can be opened from. */
  export let zIndex: string | number = 'var(--z-modal)';
  /**
   * Let an inner dropdown escape the body's scroll clip.
   *
   * Only for modals whose content fits without scrolling; otherwise the body
   * stops scrolling and long content is unreachable.
   */
  export let allowOverflow = false;
  /**
   * Whether clicking the backdrop dismisses the modal.
   *
   * Set `false` for dialogs holding in-progress user work that a stray click
   * must not destroy — the upload wizard (#739) carries a chosen file plus
   * tags, collections and speaker settings. Escape and the X button still
   * close, so there is always an obvious way out.
   */
  export let closeOnBackdropClick = true;
  export let onClose: () => void = () => {};

  /** Id of the <h2> title. Generated when omitted; pass one only where a test or a
   *  `aria-labelledby` consumer outside this component needs a stable value. */
  export let titleId = '';

  // Stable id for wiring the dialog's accessible name to its <h2> title (when no
  // custom header slot is supplied). Generated once per instance.
  const generatedTitleId = `modal-title-${Math.random().toString(36).slice(2, 10)}`;
  $: resolvedTitleId = titleId || generatedTitleId;

  const stackToken = Symbol('modal');

  function leaveStack() {
    const at = openStack.indexOf(stackToken);
    if (at !== -1) openStack.splice(at, 1);
  }

  let _wasOpen = false;
  $: if (typeof document !== 'undefined' && isOpen !== _wasOpen) {
    if (isOpen) {
      lockScroll();
      openStack.push(stackToken);
    } else {
      unlockScroll();
      leaveStack();
    }
    _wasOpen = isOpen;
  }

  onDestroy(() => {
    if (_wasOpen) unlockScroll();
    leaveStack();
  });

  function handleBackdropClick(event: MouseEvent) {
    if (!closeOnBackdropClick) return;
    if (event.target === event.currentTarget) onClose();
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key !== 'Escape' || !isOpen) return;
    if (openStack[openStack.length - 1] !== stackToken) return;
    onClose();
  }
</script>

<svelte:window on:keydown={handleKeydown} />

{#if isOpen}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-backdrop" style="z-index: {zIndex}; --modal-instance-z-index: {zIndex};" on:click={handleBackdropClick} on:wheel|preventDefault|self on:touchmove|preventDefault|self>
    <div
      class="modal-container"
      class:fullscreen
      style="{fullscreen ? '' : `max-width: ${maxWidth}`}{height && !fullscreen ? `; height: ${height}` : ''}"
      role="dialog"
      aria-modal="true"
      aria-labelledby={$$slots.header ? undefined : resolvedTitleId}
      aria-label={$$slots.header ? title || undefined : undefined}
      use:focusTrap={{ enabled: isOpen }}
    >
      <div class="modal-header">
        {#if $$slots.header}
          <div class="modal-header-content">
            <slot name="header" />
          </div>
        {:else}
          <h2 id={resolvedTitleId}>{title}</h2>
        {/if}
        {#if $$slots['header-actions']}
          <div class="modal-header-actions">
            <slot name="header-actions" />
          </div>
        {/if}
        <button class="modal-close-button" on:click={onClose} aria-label={$t('modal.closeDialog')}>
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>
      <div class="modal-body" class:allow-overflow={allowOverflow}>
        <slot />
      </div>
      {#if $$slots.footer}
        <div class="modal-footer">
          <slot name="footer" />
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  /* z-index is set via the inline `style="z-index: {zIndex}"` above, not here
     (H5) — a CSS rule here would always lose to that inline style anyway, so
     one used to sit here dead, unreachable, and confusing to a reader. */
  .modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background-color: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    overscroll-behavior: none;
  }

  .modal-container {
    background: var(--bg-primary, #ffffff);
    border: 1px solid var(--border-color, #e0e0e0);
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    width: 90%;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    animation: slideIn 0.2s ease-out;
  }

  .modal-container.fullscreen {
    width: 100%;
    height: 100%;
    height: 100dvh;
    max-height: none;
    border: none;
    border-radius: 0;
    box-shadow: none;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.5rem;
    border-bottom: 1px solid var(--border-color, #e0e0e0);
    flex-shrink: 0;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.25rem;
    color: var(--text-primary, #1a1a1a);
  }

  .modal-header-content {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex: 1;
    min-width: 0;
    gap: 0.75rem;
  }

  .modal-header-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-inline-start: auto;
    padding-inline-start: 1rem;
    flex-shrink: 0;
  }

  .modal-close-button {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-secondary, #666);
    padding: 0.5rem;
    margin-inline-start: 1rem;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: color 0.2s ease, background-color 0.2s ease;
    flex-shrink: 0;
  }

  .modal-header-actions + .modal-close-button {
    margin-inline-start: 0.5rem;
  }

  .modal-close-button:hover {
    color: var(--text-color, var(--text-primary, #1a1a1a));
    background-color: var(--button-hover, var(--bg-hover, #f0f0f0));
  }

  .modal-body {
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
    overscroll-behavior: contain;
  }

  /* An absolutely-positioned dropdown inside a scrolling body is clipped by
     that scroll container no matter how high its z-index, so it renders as a
     sliver under the footer. `allowOverflow` drops the clip for modals whose
     content is short enough not to need scrolling but which do contain a
     combobox — SearchableSelect in the bulk tag modal is the case this exists
     for. */
  .modal-body.allow-overflow {
    overflow: visible;
  }

  .modal-footer {
    display: flex;
    gap: 0.75rem;
    padding: 1rem 1.5rem;
    justify-content: flex-end;
    border-top: 1px solid var(--border-color, #e0e0e0);
    flex-shrink: 0;
  }

  /* One footer-button size for every modal. The footer used to inherit whichever of three
     vocabularies the caller used (`.btn`, `.modal-button`, an ad-hoc class), which made the
     same Cancel/Save row a different height and radius from modal to modal. Colour stays
     the caller's (`btn-primary`, `btn-secondary`, `btn-danger`...); size, radius and weight
     do not. A text-link style button opts out by having "link" in its class name. Order is
     the callers': dismiss first, the primary action last. */
  .modal-footer :global(button:not([class*='link'])) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    min-height: 2.375rem;
    padding: 0.5rem 1rem;
    border-radius: 8px;
    font-size: 0.875rem;
    font-weight: 500;
  }

  :global([data-theme='dark']) .modal-backdrop {
    background-color: rgba(0, 0, 0, 0.65);
  }

  @keyframes slideIn {
    from {
      opacity: 0;
      transform: translateY(-10px) scale(0.98);
    }
    to {
      opacity: 1;
      transform: translateY(0) scale(1);
    }
  }

  /* Raise modals above the navbar (--z-navbar) on mobile/tablet so the close
     button is not hidden behind it. `!important` is required here: this must
     win over the inline `style="z-index: {zIndex}"` when a caller passed a
     LOWER explicit zIndex, and `!important` on a normal CSS property beats an
     inline style — a plain declaration would not.

     ⚠️ Adversarial-review follow-up (H5): an unconditional
     `z-index: var(--z-modal) !important` here defeated the documented `zIndex`
     prop contract on every viewport ≤1200px — a caller passing a HIGHER value
     on purpose (e.g. `SelectiveReprocessModal` passes `--z-toast`, 9999, so it
     layers above another modal it can be opened from) got silently clobbered
     back down to `--z-modal` (1300) the moment the viewport narrowed, with no
     visual regression on desktop to catch it. `--modal-instance-z-index`
     mirrors the same inline `zIndex` value into a CSS custom property so this
     rule can take the GREATER of the two instead of unconditionally
     overwriting: a caller's default (`--z-modal`) is unaffected, a
     legitimately higher value survives, and the floor this rule exists for
     (never render below the navbar) still holds. */
  @media (max-width: 1200px) {
    .modal-backdrop {
      z-index: max(var(--z-modal), var(--modal-instance-z-index, var(--z-modal))) !important;
    }
  }

  @media (max-width: 768px) {
    .modal-container {
      width: 100%;
      max-width: 100% !important;
      max-height: 100%;
      max-height: 100dvh;
      border-radius: 0;
      margin: 0;
    }

    .modal-backdrop {
      align-items: stretch;
    }

    .modal-header {
      padding: 1rem;
      padding-top: calc(1rem + env(safe-area-inset-top, 0px));
    }

    .modal-header h2 {
      font-size: 1.1rem;
    }

    .modal-body {
      padding: 1rem;
    }

    .modal-footer {
      padding: 0.75rem 1rem;
      padding-bottom: calc(0.75rem + env(safe-area-inset-bottom, 0px));
      flex-wrap: wrap;
    }

    .modal-footer :global(button) {
      min-height: 44px;
    }
  }
</style>
