/**
 * The settings menu of the audio player is a short bar (about 74 px tall). Bounded by the
 * player, the library sized the open menu to the room left above the gear: 29 px, so Speed
 * was cut off. Video.js's own audio skins open the menu against the viewport
 * (`boundary="viewport"` on `<media-menu>`); the video player keeps the default (the video).
 */
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import SettingsMenu from './SettingsMenu.svelte';

describe('SettingsMenu boundary', () => {
  it('bounds the audio player menu by the viewport, not the player bar', () => {
    const { container } = render(SettingsMenu, { props: { id: 'a', audio: true } });
    expect(container.querySelector('media-menu')?.getAttribute('boundary')).toBe('viewport');
  });

  it('keeps the video player menu inside the video', () => {
    const { container } = render(SettingsMenu, { props: { id: 'v', audio: false } });
    expect(container.querySelector('media-menu')?.hasAttribute('boundary')).toBe(false);
  });
});

describe('SettingsMenu speed values in right-to-left languages', () => {
  // Arabic showed "×1" and "×0.5": a rate is a technical value, read left to right.
  it('keeps the speed hint and every rate label left to right', () => {
    const { container } = render(SettingsMenu, { props: { id: 's', audio: true } });
    const speedRow = container.querySelector('media-menu-item[commandfor="s-speed"]');
    expect(speedRow?.querySelector('[data-part="hint"]')?.getAttribute('dir')).toBe('ltr');
    const template = container.querySelector(
      'media-playback-rate-radio-group template'
    ) as HTMLTemplateElement;
    expect(template.content.querySelector('[data-part="label"]')?.getAttribute('dir')).toBe('ltr');
  });
});
