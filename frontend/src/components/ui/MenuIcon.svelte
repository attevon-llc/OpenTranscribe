<script lang="ts">
  /**
   * The one inline SVG icon: in front of a menu item or a button label, and as a status glyph
   * (upload rows, toasts, health tables, unsaved-changes dots). Decorative, so it is
   * `aria-hidden`: where the glyph alone carries meaning the caller names it (`aria-label` /
   * `title` on the wrapper, or visible text beside it). Colour is `currentColor`, so hover,
   * disabled, dark mode and status colours need nothing from here.
   *
   * `size` is a pixel number (14 is the gallery menus' size) or a token (`xs|sm|md|lg|xl`,
   * `--icon-size-*` in `styles/theme.css`). The box is fixed either way, so swapping one glyph
   * for another never shifts the layout. Directional glyphs (`RTL_MIRRORED`) flip in RTL.
   * `spin` rotates the glyph (the `status-working` loader) unless reduced motion is requested.
   */
  import {
    MENU_ICONS,
    ICON_SIZE_PX,
    RTL_MIRRORED,
    type IconSizeToken,
    type MenuIconName,
  } from '$lib/ui/menuIcons';

  export let name: MenuIconName;
  export let size: number | IconSizeToken = 14;
  export let spin = false;
  let className = '';
  export { className as class };

  $: px = typeof size === 'number' ? size : ICON_SIZE_PX[size];
  $: boxStyle =
    typeof size === 'number'
      ? undefined
      : `width: var(--icon-size-${size}, ${px}px); height: var(--icon-size-${size}, ${px}px)`;
</script>

<svg
  class="menu-icon {className}"
  class:mirror={RTL_MIRRORED.has(name)}
  class:spin
  xmlns="http://www.w3.org/2000/svg"
  width={px}
  height={px}
  style={boxStyle}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="2"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
  focusable="false"
  data-icon={name}
>
  {#each MENU_ICONS[name] as [tag, attrs]}
    <svelte:element this={tag} {...attrs} />
  {/each}
</svg>

<style>
  .menu-icon {
    flex-shrink: 0;
  }

  :global([dir='rtl']) .menu-icon.mirror {
    transform: scaleX(-1);
  }

  .menu-icon.spin {
    animation: menu-icon-spin 1s linear infinite;
  }

  @keyframes menu-icon-spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .menu-icon.spin {
      animation: none;
    }
  }
</style>
