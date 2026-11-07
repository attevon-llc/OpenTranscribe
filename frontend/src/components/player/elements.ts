/**
 * Registers every Video.js 10 element the composed player renders, and the icons it draws.
 *
 * Each `@videojs/html/ui/*` import defines exactly one custom element, and a compound
 * component needs an import for every part it renders, or that part stays a plain element
 * with no state ("Where to put your components",
 * https://videojs.org/docs/framework/html/guides/ui-components). Only the icons named here
 * ship (`registerIcons`, https://videojs.org/docs/framework/html/reference/components/icons).
 *
 * Imported statically by `MediaPlayer.svelte`, so the definitions run before Svelte creates
 * the elements and they upgrade in place. The player is route-scoped: none of this may reach
 * the first-load closure (`scripts/check-bundle-budget.mjs` fails the build if it does).
 */
import '@videojs/html/i18n';
import '@videojs/html/video/player';
import '@videojs/html/audio/player';
import '@videojs/html/ui/container';
import '@videojs/html/ui/controls';
import '@videojs/html/ui/controls-content';
import '@videojs/html/ui/controls-group';
import '@videojs/html/ui/play-button';
import '@videojs/html/ui/mute-button';
import '@videojs/html/ui/volume-slider';
import '@videojs/html/ui/time-slider';
import '@videojs/html/ui/slider-track';
import '@videojs/html/ui/slider-buffer';
import '@videojs/html/ui/slider-fill';
import '@videojs/html/ui/slider-thumb';
import '@videojs/html/ui/slider-preview';
import '@videojs/html/ui/slider-value';
import '@videojs/html/ui/time';
import '@videojs/html/ui/time-group';
import '@videojs/html/ui/time-separator';
import '@videojs/html/ui/captions-button';
import '@videojs/html/ui/pip-button';
import '@videojs/html/ui/airplay-button';
import '@videojs/html/ui/fullscreen-button';
import '@videojs/html/ui/menu';
import '@videojs/html/ui/menu-content';
import '@videojs/html/ui/menu-item';
import '@videojs/html/ui/menu-separator';
import '@videojs/html/ui/menu-radio-item';
import '@videojs/html/ui/menu-item-indicator';
import '@videojs/html/ui/playback-rate-radio-group';
import '@videojs/html/ui/captions-radio-group';
import '@videojs/html/ui/tooltip';
import '@videojs/html/ui/tooltip-group';
import '@videojs/html/ui/tooltip-label';
import '@videojs/html/ui/hotkey';
import '@videojs/html/ui/gesture';
import '@videojs/html/ui/status-announcer';
import '@videojs/html/ui/error-dialog';
import '@videojs/html/ui/dialog-popup';
import '@videojs/html/ui/dialog-title';
import '@videojs/html/ui/dialog-description';
import '@videojs/html/ui/dialog-close';
import {
  airPlayEnterIcon,
  airPlayExitIcon,
  captionsOffIcon,
  captionsOnIcon,
  checkIcon,
  chevronIcon,
  fullscreenEnterIcon,
  fullscreenExitIcon,
  gearIcon,
  pauseIcon,
  pipEnterIcon,
  pipExitIcon,
  playIcon,
  registerIcons,
  restartIcon,
  speedIcon,
  volumeHighIcon,
  volumeLowIcon,
  volumeOffIcon,
} from '@videojs/html/icons';

registerIcons('default', {
  'airplay-enter': airPlayEnterIcon,
  'airplay-exit': airPlayExitIcon,
  'captions-off': captionsOffIcon,
  'captions-on': captionsOnIcon,
  check: checkIcon,
  chevron: chevronIcon,
  'fullscreen-enter': fullscreenEnterIcon,
  'fullscreen-exit': fullscreenExitIcon,
  gear: gearIcon,
  pause: pauseIcon,
  'pip-enter': pipEnterIcon,
  'pip-exit': pipExitIcon,
  play: playIcon,
  restart: restartIcon,
  speed: speedIcon,
  'volume-high': volumeHighIcon,
  'volume-low': volumeLowIcon,
  'volume-off': volumeOffIcon,
});
