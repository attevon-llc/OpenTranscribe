<!--
  HiddenBadge.svelte - the "Hidden" pill on a gallery card or row, with its Unhide button
  (issue #1102).

  Renders nothing unless the caller has hidden this file, which the list only reports when it
  was asked to include hidden files. Hiding is the caller's own view preference, so the badge
  says nothing to anyone else and Unhide changes nothing for anyone else.
-->
<script lang="ts">
  import Badge from '$components/ui/Badge.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { HiddenMediaApi } from '$lib/api/hiddenMedia';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { galleryStore } from '$stores/gallery';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import type { MediaFile } from '$lib/types/media';

  export let file: MediaFile;
  /** False inside a list row, which is one link: a button may not sit inside it. The bulk
   * bar's "Show in my view" unhides from there. */
  export let showUnhide = true;

  let busy = false;

  async function unhide(event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    if (busy) return;
    busy = true;
    try {
      await HiddenMediaApi.setHidden(file.uuid, false);
      toastStore.success($t('gallery.hidden.unhiddenOne'));
      galleryStore.notifyHiddenChanged();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('gallery.hidden.unhideFailed')));
    } finally {
      busy = false;
    }
  }
</script>

{#if file.is_hidden}
  <span class="hidden-badge" data-testid="hidden-badge">
    <Badge title={$t('gallery.hidden.badgeTitle')}>
      <MenuIcon name="eye-off" size={12} />
      {$t('gallery.hidden.badge')}
    </Badge>
    {#if showUnhide}
    <button
      type="button"
      class="unhide-btn"
      on:click={unhide}
      disabled={busy}
      title={$t('gallery.hidden.unhideTooltip')}
    >
      {$t('gallery.hidden.unhide')}
    </button>
    {/if}
  </span>
{/if}

<style>
  .hidden-badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
  }

  .unhide-btn {
    background: none;
    border: none;
    padding: 0;
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    color: var(--primary-on-surface);
    cursor: pointer;
    text-decoration: underline;
  }

  .unhide-btn:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .unhide-btn:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: var(--space-0-5);
    border-radius: var(--radius-xs);
  }
</style>
