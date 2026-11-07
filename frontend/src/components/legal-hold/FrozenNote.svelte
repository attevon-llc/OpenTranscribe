<!--
  FrozenNote.svelte - the visible reason beside a control a legal hold has disabled.

  A disabled control's tooltip is often never seen (it does not show on touch, and many
  browsers fire no hover on a disabled button), so the reason is printed next to it, the way
  the comment form says why Add is disabled. Give the disabled control
  `aria-describedby={id}` so a screen reader announces the same sentence.
-->
<script lang="ts">
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { t } from '$stores/locale';

  /** A fixed id; point the disabled control's `aria-describedby` at it. */
  export let id: string;
  /** A more specific sentence than the default "locked by a legal hold". */
  export let message = '';
</script>

<p class="frozen-note" {id} data-testid="frozen-note">
  <span class="icon" aria-hidden="true"><MenuIcon name="shield" size={12} /></span>
  <span>{message || $t('holds.frozenControl')}</span>
</p>

<style>
  .frozen-note {
    display: inline-flex;
    align-items: flex-start;
    gap: var(--space-1);
    margin: 0;
    font-size: var(--font-size-xs);
    line-height: var(--line-height-snug);
    color: var(--color-warning-text);
  }

  .icon {
    display: inline-flex;
    flex-shrink: 0;
    padding-block-start: var(--space-0-5);
  }
</style>
