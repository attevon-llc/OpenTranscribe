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

// `translateSpeakerLabel` maps `SPEAKER_02` through i18next's
// `speaker.localizedLabel`, and i18next is not initialised under vitest — so the
// real helper returns an EMPTY string for exactly the labels these tests are about.
// Identity keeps the assertions about THIS component's fallback chain rather than
// about i18next bootstrapping.
vi.mock('$lib/i18n', () => ({ translateSpeakerLabel: (name: string) => name }));

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

/**
 * Issue #740. "Add speaker" used to POST a `getNextSpeakerName()` auto-label the
 * instant it was clicked: the user was never offered a field to type a name, and
 * because nothing in the segment row changed they read it as a no-op and clicked
 * again — minting an orphan `SPEAKER_NN` row per click. Naming must come FIRST,
 * and the created speaker must reach the segment.
 */
function nameDialogInput(): HTMLInputElement | null {
  return document.querySelector('.new-speaker-name-input');
}

function nameDialogConfirm(): HTMLElement | null {
  return document.querySelector('.create-speaker-confirm');
}

describe('create new speaker', () => {
  it('opens a naming dialog instead of immediately creating an auto-named speaker', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: {
        segment: segment(),
        speakers: [speaker({ name: 'SPEAKER_00' }), speaker({ name: 'SPEAKER_02' })],
        mediaFileUuid: 'file-1',
      },
    });

    await fireEvent.click(trigger(container));
    await fireEvent.click(
      portalMenu()!.querySelector('[data-action="create-speaker"]') as HTMLElement
    );
    await tick();

    // The whole point of #740: no speaker exists yet at this moment.
    expect(mockAxios.post).not.toHaveBeenCalled();
    expect(nameDialogInput()).not.toBeNull();
    // The dropdown itself gets out of the way so the dialog is usable.
    expect(portalMenu()).toBeNull();
  });

  it('creates the speaker under the name the user typed, keeping the SPEAKER_NN slot', async () => {
    mockAxios.post.mockResolvedValue({
      data: { uuid: 'spk-new', name: 'SPEAKER_03', display_name: 'Evelyn Marchetti' },
    });
    const { container } = render(SegmentSpeakerDropdown, {
      props: {
        segment: segment(),
        speakers: [speaker({ name: 'SPEAKER_00' }), speaker({ name: 'SPEAKER_02' })],
        mediaFileUuid: 'file-1',
      },
    });

    await fireEvent.click(trigger(container));
    await fireEvent.click(
      portalMenu()!.querySelector('[data-action="create-speaker"]') as HTMLElement
    );
    await tick();

    const input = nameDialogInput()!;
    await fireEvent.input(input, { target: { value: 'Evelyn Marchetti' } });
    await fireEvent.click(nameDialogConfirm()!);
    await tick();

    // `name` stays the diarization slot (speaker colours hash it); the human's
    // label goes in `display_name`, which is also what marks the row verified.
    expect(mockAxios.post).toHaveBeenCalledWith('/speakers?media_file_uuid=file-1', {
      name: 'SPEAKER_03',
      display_name: 'Evelyn Marchetti',
    });
    // ...and the dialog closes on success, so a second confirm can't double-create.
    await tick();
    expect(nameDialogInput()).toBeNull();
  });

  it('refuses to create a speaker with a blank name', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [], mediaFileUuid: 'file-1' },
    });

    await fireEvent.click(trigger(container));
    await fireEvent.click(
      portalMenu()!.querySelector('[data-action="create-speaker"]') as HTMLElement
    );
    await tick();

    await fireEvent.input(nameDialogInput()!, { target: { value: '   ' } });
    await fireEvent.click(nameDialogConfirm()!);
    await tick();

    expect(mockAxios.post).not.toHaveBeenCalled();
    expect(nameDialogInput()).not.toBeNull();
  });

  it('dispatches speakerCreated and change carrying the new speaker OBJECT, then closes', async () => {
    const created = { uuid: 'spk-new', name: 'SPEAKER_00', display_name: 'Ana' };
    mockAxios.post.mockResolvedValue({ data: created });
    const onCreated = vi.fn();
    const onChange = vi.fn();
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [], mediaFileUuid: 'file-1' },
      events: { speakerCreated: onCreated, change: onChange },
    } as never);

    await fireEvent.click(trigger(container));
    await fireEvent.click(
      portalMenu()!.querySelector('[data-action="create-speaker"]') as HTMLElement
    );
    await tick();
    await fireEvent.input(nameDialogInput()!, { target: { value: 'Ana' } });
    await fireEvent.click(nameDialogConfirm()!);
    await tick();
    await tick();

    expect(onCreated).toHaveBeenCalledWith(
      expect.objectContaining({ detail: { speaker: created } })
    );
    // The speaker object rides along: the parent's optimistic patch looks the new
    // speaker up in `speakerList`, which cannot contain it yet, so without this the
    // segment renders "Unknown" until a round trip — the "nothing happened" report.
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        detail: { segmentUuid: 'seg-1', speakerUuid: 'spk-new', speaker: created },
      })
    );
    expect(nameDialogInput()).toBeNull();
  });

  it('refuses a typed name that is itself a SPEAKER_NN placeholder', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [], mediaFileUuid: 'file-1' },
    });

    await fireEvent.click(trigger(container));
    await fireEvent.click(
      portalMenu()!.querySelector('[data-action="create-speaker"]') as HTMLElement
    );
    await tick();

    // Per the shared contract in `$lib/utils/speakerNames`, this is a diarization
    // placeholder, not an identity — accepting it recreates the #740 state.
    await fireEvent.input(nameDialogInput()!, { target: { value: 'SPEAKER_07' } });
    await fireEvent.click(nameDialogConfirm()!);
    await tick();

    expect(mockAxios.post).not.toHaveBeenCalled();
    expect(document.querySelector('.new-speaker-error')).not.toBeNull();
  });

  it('cancels without creating anything', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment(), speakers: [], mediaFileUuid: 'file-1' },
    });

    await fireEvent.click(trigger(container));
    await fireEvent.click(
      portalMenu()!.querySelector('[data-action="create-speaker"]') as HTMLElement
    );
    await tick();
    await fireEvent.click(document.querySelector('.create-speaker-cancel') as HTMLElement);
    await tick();

    expect(mockAxios.post).not.toHaveBeenCalled();
    expect(nameDialogInput()).toBeNull();
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
    await fireEvent.click(
      portalMenu()!.querySelector('[data-action="create-speaker"]') as HTMLElement
    );
    await tick();
    await fireEvent.input(nameDialogInput()!, { target: { value: 'Ana' } });
    await fireEvent.click(nameDialogConfirm()!);
    await tick();

    expect(consoleError).toHaveBeenCalledWith('Failed to create new speaker:', expect.any(Error));
    expect(mockToast.error).toHaveBeenCalledWith('common.somethingWentWrong');
    consoleError.mockRestore();
  });
});

/**
 * Issue #741. The name slot fell through
 * `display_name || name || speaker_label` with no confirmed-vs-suggested
 * distinction, so anything that put a machine guess where a human label goes
 * rendered as if a person had confirmed it. The repo rule is absolute: "LLM
 * speaker-ID suggestions are never auto-applied — they are surfaced with
 * confidence scores for manual verification only." `display_name` is the field
 * that means a human confirmed the name (`POST/PUT /speakers` flips `verified`
 * the moment it is set); `suggested_name` + `confidence` + `suggestion_source`
 * is the machine's guess and must never occupy the name slot. Note that
 * `resolved_display_name` / `resolved_speaker_name` deliberately COLLAPSE the
 * two (`canonical_speaker_label` returns `suggested_name` at confidence >= 0.75),
 * which is exactly why neither may be rendered here.
 */
const suggestedSpeaker = {
  uuid: 'spk-sug',
  name: 'SPEAKER_02',
  suggested_name: 'Dr. Evelyn Marchetti-Whitfield',
  suggestion_source: 'llm_analysis',
  confidence: 0.92,
  verified: false,
};

// The same speaker after a human confirmed a name. `display_name` set is what
// `POST/PUT /speakers` treats as confirmation — it flips `verified` server-side.
const confirmedSpeaker = {
  ...suggestedSpeaker,
  display_name: 'Alice Nakamura',
  verified: true,
};

describe('unconfirmed LLM speaker suggestions', () => {
  it('shows the SPEAKER_NN label, never the unconfirmed suggestion, in the name slot', () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment({ speaker: suggestedSpeaker }) },
    });

    const label = trigger(container).textContent ?? '';
    expect(label).toContain('SPEAKER_02');
    expect(label).not.toContain('Evelyn');
  });

  it('shows a confirmed display_name in the name slot, in preference to a suggestion', () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: {
        segment: segment({ speaker: confirmedSpeaker }),
      },
    });

    const label = trigger(container).textContent ?? '';
    expect(label).toContain('Alice Nakamura');
    expect(label).not.toContain('Evelyn');
  });

  it('offers the suggestion separately in the menu, with its confidence score', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment({ speaker: suggestedSpeaker }), speakers: [] },
    });

    await fireEvent.click(trigger(container));
    const row = portalMenu()!.querySelector('[data-action="accept-suggestion"]');

    expect(row).not.toBeNull();
    expect(row!.textContent).toContain('Dr. Evelyn Marchetti-Whitfield');
    expect(row!.textContent).toContain('92%');
  });

  it('accepting a suggestion dispatches speakerUpdate — it never writes the name itself', async () => {
    const onSpeakerUpdate = vi.fn();
    const { container } = render(SegmentSpeakerDropdown, {
      props: { segment: segment({ speaker: suggestedSpeaker }), speakers: [] },
      events: { speakerUpdate: onSpeakerUpdate },
    } as never);

    await fireEvent.click(trigger(container));
    await fireEvent.click(
      portalMenu()!.querySelector('[data-action="accept-suggestion"]') as HTMLElement
    );
    await tick();

    expect(onSpeakerUpdate).toHaveBeenCalledWith(
      expect.objectContaining({
        detail: { speakerId: 'spk-sug', newName: 'Dr. Evelyn Marchetti-Whitfield' },
      })
    );
    expect(mockAxios.post).not.toHaveBeenCalled();
  });

  it('offers no suggestion row once a human has confirmed a name', async () => {
    const { container } = render(SegmentSpeakerDropdown, {
      props: {
        segment: segment({ speaker: confirmedSpeaker }),
        speakers: [],
      },
    });

    await fireEvent.click(trigger(container));

    expect(portalMenu()!.querySelector('[data-action="accept-suggestion"]')).toBeNull();
  });
});

describe('reassigning to an existing speaker', () => {
  it('re-renders the trigger when the parent commits the new speaker onto the segment', async () => {
    const onChange = vi.fn();
    const { container, rerender } = render(SegmentSpeakerDropdown, {
      props: {
        segment: segment({ speaker: { uuid: 's1', name: 'SPEAKER_00' } }),
        speakers: [speaker({ uuid: 's2', name: 'SPEAKER_01', display_name: 'Bob Kowalski' })],
      },
      events: { change: onChange },
    } as never);

    await fireEvent.click(trigger(container));
    await fireEvent.click(
      portalMenu()!.querySelector('button[data-speaker-uuid="s2"]') as HTMLElement
    );

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ detail: expect.objectContaining({ speakerUuid: 's2' }) })
    );

    // The parent owns the write; this asserts the component reflects it once made.
    await rerender({
      segment: segment({
        speaker: { uuid: 's2', name: 'SPEAKER_01', display_name: 'Bob Kowalski' },
      }),
    });

    expect(trigger(container).textContent).toContain('Bob Kowalski');
  });
});
