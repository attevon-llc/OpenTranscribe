<script lang="ts">
  /**
   * The settings button and its menu: Speed (every player) and Captions (video), as
   * submenus of one `<media-menu>` (https://videojs.org/docs/framework/html/reference/components/menu).
   * The radio groups build their own items from player state: the speed list the player
   * offers (`default-playback-rates`, set by `MediaPlayer`) and the caption tracks plus Off.
   * The library positions the menu (it mirrors `align` in RTL), so nothing here sets
   * `position`.
   */
  export let audio = false;
  /** Unique id of the trigger button; the menu ids derive from it. */
  export let id: string;

  $: menuId = `${id}-menu`;
  $: speedId = `${id}-speed`;
  $: captionsId = `${id}-captions`;
</script>

<button type="button" id={id} class="control settings" commandfor={menuId}>
  <media-icon name="gear"></media-icon>
  <media-text class="visually-hidden" token="menu.settings">Settings</media-text>
</button>
<media-tooltip trigger={id} side="top" class="tip"><media-text token="menu.settings">Settings</media-text></media-tooltip>
<media-menu
  id={menuId}
  side="top"
  align="end"
  boundary={audio ? 'viewport' : undefined}
  class="settings-menu"
>
  <media-menu-content class="menu-view">
    <media-menu-item commandfor={speedId} class="menu-row">
      <media-icon name="speed" class="menu-icon"></media-icon>
      <media-text token="menu.speed">Speed</media-text>
      <span class="menu-hint"><span data-part="hint" dir="ltr"></span><media-icon name="chevron" class="chevron-forward"></media-icon></span>
    </media-menu-item>
    {#if !audio}
      <media-menu-item commandfor={captionsId} class="menu-row">
        <media-icon name="captions-off" class="menu-icon"></media-icon>
        <media-text token="menu.captions">Captions</media-text>
        <span class="menu-hint"><span data-part="hint"></span><media-icon name="chevron" class="chevron-forward"></media-icon></span>
      </media-menu-item>
    {/if}
  </media-menu-content>
  <media-menu-content id={speedId} class="menu-view">
    <media-menu-item class="menu-row menu-back">
      <media-icon name="chevron" class="chevron-back"></media-icon>
      <media-text token="menu.speed">Speed</media-text>
    </media-menu-item>
    <media-menu-separator class="menu-separator"></media-menu-separator>
    <media-playback-rate-radio-group class="menu-group">
      <template>
        <media-menu-radio-item class="menu-row"><span data-part="label" dir="ltr"></span><media-menu-item-indicator force-mount class="menu-check"><media-icon name="check"></media-icon></media-menu-item-indicator></media-menu-radio-item>
      </template>
    </media-playback-rate-radio-group>
  </media-menu-content>
  {#if !audio}
    <media-menu-content id={captionsId} class="menu-view">
      <media-menu-item class="menu-row menu-back">
        <media-icon name="chevron" class="chevron-back"></media-icon>
        <media-text token="menu.captions">Captions</media-text>
      </media-menu-item>
      <media-menu-separator class="menu-separator"></media-menu-separator>
      <media-captions-radio-group class="menu-group">
        <template>
          <media-menu-radio-item class="menu-row"><span data-part="label"></span><media-menu-item-indicator force-mount class="menu-check"><media-icon name="check"></media-icon></media-menu-item-indicator></media-menu-radio-item>
        </template>
      </media-captions-radio-group>
    </media-menu-content>
  {/if}
</media-menu>
