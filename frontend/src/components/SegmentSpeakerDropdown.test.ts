/**
 * `SegmentSpeakerDropdown.svelte` builds its open menu imperatively into a
 * `document.body` portal (so it isn't clipped by an ancestor's `overflow:
 * hidden`) rather than through Svelte's normal reactive rendering. That's real,
 * easy-to-get-wrong logic: the portal must be torn down on unmount, a stale
 * open menu must re-render when speakers/segment change under it, and the
 * "next speaker name" and named-vs-auto-label ordering are actual algorithms,
 * not markup.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Speaker, Segment } from '$lib/types/speaker';

const mockScrollLock = vi.hoisted(() => ({ lockScroll: vi.fn(), unlockScroll: vi.fn() }));
vi.mock('$lib/scrollLock', () => mockScrollLock);

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const mockAxios = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

const mockToast = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

import SegmentSpeakerDropdown from './SegmentSpeakerDropdown.svelte';

function speaker(overrides: Partial<Speaker> = {}): Speaker {
  return { uuid: 'spk-1', name: 'SPEAKER_00', ...overrides };
}

function segment(overrides: Partial<Segment> = {}): Segment {
  return {
    uuid: 'seg-1',
    start_time: 0,
    end_time: 1,
    text: 'hello',
    ...overrides,
  } as Segment;
}

function portalMenu(): HTMLElement | null {
  return document.querySelector('.speaker-dropdown-portal .dropdown-menu');
}

function trigger(container: HTMLElement): HTMLElement {
  return container.querySelector('.speaker-trigger') as HTMLElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = '';
});

describe('trigger label', () => {
  it('prefers display_name, then name, then speaker_label, then Unknown', () => {
    const { container, unmount } = render(SegmentSpeakerDropdown, {
      props: {
        segment: segment({ speaker: { uuid: 's1', name: 'SPEAKER_00', display_name: 'Alice' } }),
      },
    });
    expect(trigger(container).textContent).toContain('Alice');
    unmount();

    // A non-numeric label sidesteps translateSpeakerLabel's own SPEAKER_## ->
    // i18next lookup, isolating this component's own fallback-chain order
    // (speaker.name -> speaker_label) from that unrelated translation path.
    const second = render(SegmentSpeakerDropdown, {
      props: { segment: segment({ speaker_label: 'Unlabeled Voice' }) },
    });
    expect(trigger(second.container).textContent).toContain('Unlabeled Voice');
    second.unmount();

    const third = render(SegmentSpeakerDropdown, {
      props: { segment: segment() },
    });
    expect(trigger(third.container).textContent).toContain('common.unknown');
  });
});

describe('open / close', () => {
  it('opens a portal menu on trigger click and locks scroll', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [speaker()] },
    });

    await fireEvent.click(trigger(container));

    expect(portalMenu()).not.toBeNull();
    expect(mockScrollLock.lockScroll).toHaveBeenCalledTimes(1);
  });

  it('closes and unlocks scroll on a second trigger click', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [speaker()] },
    });

    await fireEvent.click(trigger(container));
    await fireEvent.click(trigger(container));

    expect(portalMenu()).toBeNull();
    expect(mockScrollLock.unlockScroll).toHaveBeenCalledTimes(1);
  });

  it('closes when clicking outside the trigger and the portal', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [speaker()] },
    });

    await fireEvent.click(trigger(container));
    expect(portalMenu()).not.toBeNull();

    const outside = document.createElement('div');
    document.body.appendChild(outside);
    await fireEvent.click(outside);

    expect(portalMenu()).toBeNull();
  });

  it('tears the portal down and unlocks scroll on unmount while open', async () => {
    const { container, unmount } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [speaker()] },
    });
    await fireEvent.click(trigger(container));
    expect(document.querySelector('.speaker-dropdown-portal')).not.toBeNull();

    unmount();

    expect(document.querySelector('.speaker-dropdown-portal')).toBeNull();
    expect(mockScrollLock.unlockScroll).toHaveBeenCalledTimes(1);
  });
});

describe('selecting a speaker', () => {
  it('dispatches change with null and closes when "No Speaker" is chosen', async () => {
    const onChange = vi.fn();
    const { container } = render(SegmentSpeakerDropdown, {
      props: {
        segment: segment({ speaker: { uuid: 's1', name: 'SPEAKER_00' } }),
        speakers: [speaker({ uuid: 's1' })],
      },
      events: { change: onChange },
    } as never);

    await fireEvent.click(trigger(container));
    const noSpeakerBtn = portalMenu()!.querySelector('button[data-speaker-uuid=""]') as HTMLElement;
    await fireEvent.click(noSpeakerBtn);

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ detail: { segmentUuid: 'seg-1', speakerUuid: null } })
    );
    expect(portalMenu()).toBeNull();
  });

  it('dispatches change with the clicked speaker uuid', async () => {
    const onChange = vi.fn();
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [speaker({ uuid: 'spk-99', display_name: 'Bob' })] },
      events: { change: onChange },
    } as never);

    await fireEvent.click(trigger(container));
    const speakerBtn = portalMenu()!.querySelector(
      'button[data-speaker-uuid="spk-99"]'
    ) as HTMLElement;
    await fireEvent.click(speakerBtn);

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ detail: { segmentUuid: 'seg-1', speakerUuid: 'spk-99' } })
    );
  });
});

describe('named vs. auto-labeled speaker ordering', () => {
  it('lists human-named speakers before raw SPEAKER_## auto-labels', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: {
        segment: segment(),
        speakers: [
          speaker({ uuid: 'auto-1', name: 'SPEAKER_00' }), // auto, listed first in props
          speaker({ uuid: 'named-1', name: 'SPEAKER_05', display_name: 'Charlie' }), // named
        ],
      },
    });

    await fireEvent.click(trigger(container));

    const uuids = Array.from(portalMenu()!.querySelectorAll('button[data-speaker-uuid]'))
      .map((b) => b.getAttribute('data-speaker-uuid'))
      .filter((u) => u); // drop the "No Speaker" entry (empty string)

    expect(uuids).toEqual(['named-1', 'auto-1']);
  });
});

describe('create new speaker', () => {
  it('computes the next SPEAKER_NN name from the highest existing number', async () => {
    mockAxios.post.mockResolvedValue({ data: { uuid: 'spk-new', name: 'SPEAKER_03' } });
    const { container } = render(SegmentSpeakerDropdown, {
      props: {
        segment: segment(),
        speakers: [speaker({ name: 'SPEAKER_00' }), speaker({ name: 'SPEAKER_02' })],
        mediaFileUuid: 'file-1',
      },
    });

    await fireEvent.click(trigger(container));
    const menu = portalMenu()!;
    // The create button's own label must already show the SAME computed name
    // the POST body will carry — the two must never diverge.
    expect(menu.querySelector('[data-action="create-speaker"]')?.textContent).toContain(
      'SPEAKER_03'
    );
    const createBtn = menu.querySelector('[data-action="create-speaker"]') as HTMLElement;
    await fireEvent.click(createBtn);
    await tick();

    expect(mockAxios.post).toHaveBeenCalledWith('/speakers?media_file_uuid=file-1', {
      name: 'SPEAKER_03',
    });
  });

  it('dispatches speakerCreated and change with the new speaker, then closes', async () => {
    mockAxios.post.mockResolvedValue({ data: { uuid: 'spk-new', name: 'SPEAKER_00' } });
    const onCreated = vi.fn();
    const onChange = vi.fn();
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [], mediaFileUuid: 'file-1' },
      events: { speakerCreated: onCreated, change: onChange },
    } as never);

    await fireEvent.click(trigger(container));
    const createBtn = portalMenu()!.querySelector('[data-action="create-speaker"]') as HTMLElement;
    await fireEvent.click(createBtn);
    await tick();
    await tick();

    expect(onCreated).toHaveBeenCalledWith(
      expect.objectContaining({ detail: { speaker: { uuid: 'spk-new', name: 'SPEAKER_00' } } })
    );
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ detail: { segmentUuid: 'seg-1', speakerUuid: 'spk-new' } })
    );
    expect(portalMenu()).toBeNull();
  });

  it('does not offer a create button when mediaFileUuid is unset', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [] },
    });

    await fireEvent.click(trigger(container));

    expect(portalMenu()!.querySelector('[data-action="create-speaker"]')).toBeNull();
  });

  it('logs and shows a toast rather than throwing when the create request fails', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    mockAxios.post.mockRejectedValue(new Error('server error'));
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [], mediaFileUuid: 'file-1' },
    });

    await fireEvent.click(trigger(container));
    const createBtn = portalMenu()!.querySelector('[data-action="create-speaker"]') as HTMLElement;
    await fireEvent.click(createBtn);
    await tick();

    expect(consoleError).toHaveBeenCalledWith('Failed to create new speaker:', expect.any(Error));
    expect(mockToast.error).toHaveBeenCalledWith('common.somethingWentWrong');
    consoleError.mockRestore();
  });
});
