<script context="module" lang="ts">
  /** Tooltips and menus link to their triggers by id, so each player needs its own. */
  const instances = { count: 0 };
</script>

<script lang="ts">
  /**
   * The player's control bar and settings menu, composed from Video.js 10 UI elements in the
   * app's own markup and design tokens (plan #1327, section 4.1). The elements bring state,
   * keyboard support, translated accessible names and `data-*` state attributes; the look is
   * ours. Anatomy follows the component reference pages
   * (https://videojs.org/docs/framework/html/guides/build-your-own-skin) and the shipped
   * default skin's markup.
   *
   * Every visible control label comes from Video.js's own translations (`aria-label` on the
   * buttons, `<media-text token>` for menu headings), so the bar follows the app locale with
   * no app copy (`$lib/player/i18n`).
   */
  import SettingsMenu from './SettingsMenu.svelte';

  /** `<audio>`: no captions, picture-in-picture, AirPlay or fullscreen controls. */
  export let audio = false;
  export let variant: 'full' | 'mini' = 'full';

  const id = `media-controls-${++instances.count}`;
  const ids = {
    play: `${id}-play`,
    mute: `${id}-mute`,
    captions: `${id}-captions`,
    pip: `${id}-pip`,
    airplay: `${id}-airplay`,
    fullscreen: `${id}-fullscreen`,
  };

  $: full = variant === 'full';
</script>

{#if !audio}
  <!-- The large centre button (the large play button); shown only while paused. -->
  <media-play-button class="big-play">
    <media-icon name="play"></media-icon>
  </media-play-button>
{/if}

<media-controls class="controls" class:audio visibility={audio ? 'always' : 'auto'}>
  <media-controls-content class="controls-content">
    <media-tooltip-group>
      <media-time-slider class="slider time-slider">
        <media-slider-track class="slider-track">
          <media-slider-buffer class="slider-buffer"></media-slider-buffer>
          <media-slider-fill class="slider-fill"></media-slider-fill>
        </media-slider-track>
        <media-slider-thumb class="slider-thumb"></media-slider-thumb>
        <media-slider-preview class="slider-preview">
          <media-slider-value type="pointer"></media-slider-value>
        </media-slider-preview>
      </media-time-slider>

      <media-controls-group class="controls-row">
        <media-play-button id={ids.play} class="control play">
          <media-icon name="restart" class="icon-restart"></media-icon>
          <media-icon name="play" class="icon-play"></media-icon>
          <media-icon name="pause" class="icon-pause"></media-icon>
        </media-play-button>
        <media-tooltip trigger={ids.play} side="top" class="tip"><media-tooltip-label></media-tooltip-label></media-tooltip>

        <media-mute-button id={ids.mute} class="control mute">
          <media-icon name="volume-off" class="icon-off"></media-icon>
          <media-icon name="volume-low" class="icon-low"></media-icon>
          <media-icon name="volume-high" class="icon-high"></media-icon>
        </media-mute-button>
        <media-tooltip trigger={ids.mute} side="top" class="tip"><media-tooltip-label></media-tooltip-label></media-tooltip>
        {#if full || audio}
          <media-volume-slider class="slider volume-slider">
            <media-slider-track class="slider-track">
              <media-slider-fill class="slider-fill"></media-slider-fill>
            </media-slider-track>
            <media-slider-thumb class="slider-thumb"></media-slider-thumb>
          </media-volume-slider>
        {/if}

        <media-time-group class="time">
          <media-time type="current"></media-time>
          <media-time-separator></media-time-separator>
          <media-time type="duration"></media-time>
        </media-time-group>

        <span class="spacer"></span>

        {#if !audio}
          <media-captions-button id={ids.captions} class="control captions">
            <media-icon name="captions-off" class="icon-off"></media-icon>
            <media-icon name="captions-on" class="icon-on"></media-icon>
          </media-captions-button>
          <media-tooltip trigger={ids.captions} side="top" class="tip"><media-tooltip-label></media-tooltip-label></media-tooltip>
        {/if}

        <SettingsMenu {audio} id={`${id}-settings`} />

        {#if !audio}
          {#if full}
            <media-pip-button id={ids.pip} class="control">
              <media-icon name="pip-enter" class="icon-enter"></media-icon>
              <media-icon name="pip-exit" class="icon-exit"></media-icon>
            </media-pip-button>
            <media-tooltip trigger={ids.pip} side="top" class="tip"><media-tooltip-label></media-tooltip-label></media-tooltip>
            <media-airplay-button id={ids.airplay} class="control">
              <media-icon name="airplay-enter" class="icon-enter"></media-icon>
              <media-icon name="airplay-exit" class="icon-exit"></media-icon>
            </media-airplay-button>
            <media-tooltip trigger={ids.airplay} side="top" class="tip"><media-tooltip-label></media-tooltip-label></media-tooltip>
          {/if}
          <media-fullscreen-button id={ids.fullscreen} class="control">
            <media-icon name="fullscreen-enter" class="icon-enter"></media-icon>
            <media-icon name="fullscreen-exit" class="icon-exit"></media-icon>
          </media-fullscreen-button>
          <media-tooltip trigger={ids.fullscreen} side="top" class="tip"><media-tooltip-label></media-tooltip-label></media-tooltip>
        {/if}
      </media-controls-group>
    </media-tooltip-group>
  </media-controls-content>
</media-controls>
