import { describe, it, expect, vi } from 'vitest';
import type { Speaker } from '$lib/types/speaker';
import {
  buildSpeakerMenu,
  createCheckmarkSvg,
  renderProfileMatches,
  type SpeakerMenuInput,
} from './speakerMenuDom';

const t = (key: string) => key;
const spk = (o: Partial<Speaker>): Speaker => ({ uuid: 'u', name: 'SPEAKER_00', ...o });

function input(o: Partial<SpeakerMenuInput> = {}): SpeakerMenuInput {
  return {
    speakers: [],
    currentSpeakerUuid: undefined,
    noSpeaker: true,
    wireSpeakerUuid: undefined,
    speakerNumberLabel: 'SPEAKER_00',
    inlineName: '',
    pendingSuggestion: null,
    canCreateSpeaker: false,
    t,
    translateLabel: (n) => n,
    onInlineInput: vi.fn(),
    onSubmitInline: vi.fn(),
    onEscape: vi.fn(),
    onAcceptSuggestion: vi.fn(),
    onCreateSpeaker: vi.fn(),
    onSelect: vi.fn(),
    ...o,
  };
}

const rowLabels = (menu: HTMLElement) =>
  [...menu.querySelectorAll('.dropdown-item .speaker-option span')].map((e) => e.textContent);

describe('buildSpeakerMenu rows', () => {
  it('lists named speakers before auto-labelled ones, after "No Speaker"', () => {
    const { menu } = buildSpeakerMenu(
      input({
        speakers: [
          spk({ uuid: 'a', name: 'SPEAKER_00' }),
          spk({ uuid: 'b', name: 'SPEAKER_01', display_name: 'John' }),
        ],
      })
    );
    expect(rowLabels(menu)).toEqual(['speaker.noSpeaker', 'John (01)', 'SPEAKER_00']);
  });

  it('checks the current speaker, or No Speaker when the segment has none', () => {
    const speakers = [spk({ uuid: 'a' })];
    const withCurrent = buildSpeakerMenu(
      input({ speakers, currentSpeakerUuid: 'a', noSpeaker: false })
    );
    expect(withCurrent.menu.querySelectorAll('.dropdown-item.selected')).toHaveLength(1);
    expect(withCurrent.menu.querySelector('.selected')?.getAttribute('data-speaker-uuid')).toBe(
      'a'
    );
    const none = buildSpeakerMenu(input({ speakers }));
    expect(none.menu.querySelector('.selected')?.getAttribute('data-speaker-uuid')).toBe('');
  });

  it('shows "Add speaker" only when a file is known', () => {
    expect(buildSpeakerMenu(input()).menu.querySelector('[data-action=create-speaker]')).toBeNull();
    expect(
      buildSpeakerMenu(input({ canCreateSpeaker: true })).menu.querySelector(
        '[data-action=create-speaker]'
      )
    ).not.toBeNull();
  });

  it('renders the suggestion with its rounded confidence', () => {
    const { menu } = buildSpeakerMenu(
      input({ pendingSuggestion: { uuid: 's', name: 'Bob', confidence: 0.876 } })
    );
    expect(menu.querySelector('.suggestion-confidence')?.textContent).toBe('88%');
    expect(menu.querySelector('[data-action=accept-suggestion] span')?.textContent).toBe('Bob');
  });

  it('writes names as text, never as markup', () => {
    const { menu } = buildSpeakerMenu(
      input({ speakers: [spk({ uuid: 'x', name: 'SPEAKER_05', display_name: '<img src=x>' })] })
    );
    expect(menu.querySelector('img')).toBeNull();
    expect(rowLabels(menu)).toContain('<img src=x> (05)');
  });
});

describe('buildSpeakerMenu callbacks', () => {
  it('selecting a speaker and "No Speaker" report the uuid / null', () => {
    const onSelect = vi.fn();
    const { menu } = buildSpeakerMenu(input({ speakers: [spk({ uuid: 'a' })], onSelect }));
    (menu.querySelector('[data-speaker-uuid=""]') as HTMLElement).click();
    (menu.querySelector('[data-speaker-uuid="a"]') as HTMLElement).click();
    expect(onSelect.mock.calls).toEqual([[null], ['a']]);
  });
});

describe('buildSpeakerMenu inline name field', () => {
  it('exists only for a segment that has a speaker', () => {
    const none = buildSpeakerMenu(input());
    expect(none.inlineInput).toBeNull();
    expect(none.inlineSaveButton).toBeNull();
    const some = buildSpeakerMenu(input({ wireSpeakerUuid: 's1', inlineName: 'Lau' }));
    expect(some.inlineInput?.value).toBe('Lau');
    expect(some.inlineSaveButton?.dataset.action).toBe('save-inline-name');
    expect(some.inlineHint?.hidden).toBe(true);
    expect(some.inlineMatches?.hidden).toBe(true);
  });

  it('Enter submits, Escape closes, and keystrokes never reach the page shortcuts', () => {
    const onSubmitInline = vi.fn();
    const onEscape = vi.fn();
    const pageShortcut = vi.fn();
    document.body.addEventListener('keydown', pageShortcut);
    const { menu, inlineInput } = buildSpeakerMenu(
      input({ wireSpeakerUuid: 's1', onSubmitInline, onEscape })
    );
    document.body.appendChild(menu);
    inlineInput?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    inlineInput?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    inlineInput?.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));
    expect(onSubmitInline).toHaveBeenCalledTimes(1);
    expect(onEscape).toHaveBeenCalledTimes(1);
    expect(pageShortcut).not.toHaveBeenCalled();
    document.body.removeEventListener('keydown', pageShortcut);
    menu.remove();
  });

  it('reports typing with the field value', () => {
    const onInlineInput = vi.fn();
    const { inlineInput } = buildSpeakerMenu(input({ wireSpeakerUuid: 's1', onInlineInput }));
    inlineInput!.value = 'Laura';
    inlineInput!.dispatchEvent(new Event('input'));
    expect(onInlineInput).toHaveBeenCalledWith('Laura');
  });
});

describe('renderProfileMatches', () => {
  it('replaces the list in place and reports a picked name', () => {
    const container = document.createElement('div');
    const onPick = vi.fn();
    renderProfileMatches(container, ['Laura', 'Lars'], 'matches', onPick);
    expect(container.hidden).toBe(false);
    expect(container.querySelector('.inline-name-matches-label')?.textContent).toBe('matches');
    (container.querySelectorAll('[data-action=pick-profile]')[1] as HTMLElement).click();
    expect(onPick).toHaveBeenCalledWith('Lars');

    renderProfileMatches(container, [], 'matches', onPick);
    expect(container.hidden).toBe(true);
    expect(container.children).toHaveLength(0);
  });
});

describe('createCheckmarkSvg', () => {
  it('is a 16px stroked polyline', () => {
    const svg = createCheckmarkSvg();
    expect(svg.getAttribute('width')).toBe('16');
    expect(svg.querySelector('polyline')?.getAttribute('points')).toBe('20 6 9 17 4 12');
  });
});
