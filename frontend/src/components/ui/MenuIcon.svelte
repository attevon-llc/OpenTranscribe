<script lang="ts">
  /**
   * The icon in front of a menu item or a button label. Decorative (the label carries the
   * meaning), so it is `aria-hidden`; colour is `currentColor`, so hover, disabled, dark and
   * RTL need nothing from the caller. 14px is the gallery menus' size; pass `size` for the
   * 16px menus (user menu, transcript action bar).
   */
  import { MENU_ICONS, type MenuIconName } from '$lib/ui/menuIcons';

  export let name: MenuIconName;
  export let size = 14;
</script>

<svg
  class="menu-icon"
  xmlns="http://www.w3.org/2000/svg"
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="2"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
  focusable="false"
>
  {#each MENU_ICONS[name] as [tag, attrs]}
    <svelte:element this={tag} {...attrs} />
  {/each}
</svg>

<style>
  .menu-icon {
    flex-shrink: 0;
  }
</style>
