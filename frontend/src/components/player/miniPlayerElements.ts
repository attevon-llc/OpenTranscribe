/**
 * Registers the Video.js 10 custom elements and icons the mini player renders (issue #1327).
 *
 * Composed controls, not a packaged skin (plan section 4.1): each `@videojs/html/ui/*` entry
 * defines one element, so only what the mini player uses ships, in the chunk of the route that
 * shows it. Pattern: https://videojs.org/docs/framework/html/guides/build-your-own-skin
 * Icons are registered by name for `<media-icon>`; importing only these keeps the rest out:
 * https://videojs.org/docs/framework/html/reference/components/icons ("registerIcons").
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
import '@videojs/html/ui/time';
import '@videojs/html/ui/time-group';
import '@videojs/html/ui/time-separator';
import '@videojs/html/ui/playback-rate-button';
import '@videojs/html/ui/menu';
import '@videojs/html/ui/menu-content';
import '@videojs/html/ui/playback-rate-radio-group';
import '@videojs/html/ui/menu-radio-item';
import '@videojs/html/ui/menu-item-indicator';
import '@videojs/html/ui/fullscreen-button';
import '@videojs/html/ui/hotkey';
import '@videojs/html/ui/gesture';
import '@videojs/html/ui/tooltip';
import '@videojs/html/ui/tooltip-group';
import '@videojs/html/ui/tooltip-label';
import '@videojs/html/ui/status-announcer';
import {
  checkIcon,
  fullscreenEnterIcon,
  fullscreenExitIcon,
  pauseIcon,
  playIcon,
  registerIcons,
  restartIcon,
  volumeHighIcon,
  volumeLowIcon,
  volumeOffIcon,
} from '@videojs/html/icons';

registerIcons('default', {
  check: checkIcon,
  'fullscreen-enter': fullscreenEnterIcon,
  'fullscreen-exit': fullscreenExitIcon,
  pause: pauseIcon,
  play: playIcon,
  restart: restartIcon,
  'volume-high': volumeHighIcon,
  'volume-low': volumeLowIcon,
  'volume-off': volumeOffIcon,
});
