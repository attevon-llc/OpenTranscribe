/**
 * The caption overlay draws the showing track's cues itself (plan #1327, row 24), because
 * `::cue(.speaker)` does not style anything in Firefox. jsdom has no text tracks, so the media
 * element's `textTracks` is a small stand-in; real cues are proven by the e2e caption tests.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';
import { tick } from 'svelte';
import CaptionOverlay from './CaptionOverlay.svelte';

/** A cue as `getCueAsHTML()` returns it for `<c.speaker>Name</c>: text`. */
function cue(speaker: string, text: string) {
  return {
    getCueAsHTML() {
      const fragment = document.createDocumentFragment();
      const name = document.createElement('span');
      name.className = 'speaker';
      name.textContent = speaker;
      fragment.append(name, `: ${text}`);
      return fragment;
    },
  };
}

function track(kind: string, mode: TextTrackMode, cues: ReturnType<typeof cue>[] = []) {
  return Object.assign(new EventTarget(), { kind, mode, activeCues: cues });
}

function mediaWith(...tracks: ReturnType<typeof track>[]) {
  const list = Object.assign(new EventTarget(), { length: tracks.length }, tracks);
  const media = document.createElement('video');
  Object.defineProperty(media, 'textTracks', { value: list });
  return { media, list };
}

const lines = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('.caption-cue')).map((l) => l.textContent);

describe('CaptionOverlay', () => {
  it('draws the showing caption track’s active cues with the speaker element intact', async () => {
    const { media } = mediaWith(
      track('chapters', 'hidden', [cue('Chapter', 'not a caption')]),
      track('captions', 'showing', [cue('Alice', 'So what we found')])
    );
    const { container } = render(CaptionOverlay, { props: { media } });
    await tick();

    expect(lines(container)).toEqual(['Alice: So what we found']);
    expect(container.querySelector('.caption-cue .speaker')?.textContent).toBe('Alice');
    expect(container.querySelector('.caption-overlay')?.classList.contains('has-cues')).toBe(true);
  });

  it('follows the cue changes of the showing track', async () => {
    const captions = track('captions', 'showing', [cue('Alice', 'first')]);
    const { media } = mediaWith(captions);
    const { container } = render(CaptionOverlay, { props: { media } });

    captions.activeCues = [cue('Bob', 'second')];
    captions.dispatchEvent(new Event('cuechange'));
    await tick();
    expect(lines(container)).toEqual(['Bob: second']);

    captions.activeCues = [];
    captions.dispatchEvent(new Event('cuechange'));
    await tick();
    expect(lines(container)).toEqual([]);
    expect(container.querySelector('.caption-overlay')?.classList.contains('has-cues')).toBe(false);
  });

  it('clears when captions are switched off and draws again when they come back', async () => {
    const captions = track('captions', 'showing', [cue('Alice', 'hello')]);
    const { media, list } = mediaWith(captions);
    const { container } = render(CaptionOverlay, { props: { media } });

    captions.mode = 'disabled';
    list.dispatchEvent(new Event('change'));
    await tick();
    expect(lines(container)).toEqual([]);

    captions.mode = 'showing';
    list.dispatchEvent(new Event('change'));
    await tick();
    expect(lines(container)).toEqual(['Alice: hello']);
  });

  it('stops listening to the old element when the media changes', async () => {
    const old = track('captions', 'showing', [cue('Alice', 'old')]);
    const first = mediaWith(old);
    const second = mediaWith(track('captions', 'showing', [cue('Bob', 'new')]));
    const { container, rerender } = render(CaptionOverlay, { props: { media: first.media } });

    await rerender({ media: second.media });
    await tick();
    old.activeCues = [cue('Alice', 'late')];
    old.dispatchEvent(new Event('cuechange'));
    await tick();
    expect(lines(container)).toEqual(['Bob: new']);
  });
});
