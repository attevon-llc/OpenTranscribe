/**
 * The cluster card's header is a disclosure (WAI-ARIA APG): a real <button aria-expanded> that
 * Enter and Space toggle, holding no focusable control (axe nested-interactive).
 *
 * The old header was a `<div role="button">` with the label editor and the label's own
 * `role="button"` span inside it, and its keydown handler toggled the card on EVERY key: Tab
 * past it, or type a letter into the label input, and the cluster opened or closed.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import SpeakerClusterCard from './SpeakerClusterCard.svelte';
import type { SpeakerCluster } from '$lib/types/speakerCluster';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const cluster: SpeakerCluster = {
  uuid: 'c-1',
  label: 'Alice',
  description: null,
  user_id: 1,
  member_count: 3,
  promoted_to_profile_id: null,
  promoted_to_profile_uuid: null,
  promoted_to_profile_name: null,
  promoted_to_profile_avatar_url: null,
  suggested_name: null,
  quality_score: 0.9,
  min_similarity: null,
  separation_score: null,
  gender_composition: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

function setup() {
  const onUpdate = vi.fn();
  const onExpand = vi.fn();
  const result = render(SpeakerClusterCard, {
    props: { cluster },
    events: { update: onUpdate, expand: onExpand },
  } as never);
  const body = () => result.container.querySelector('.card-body');
  return { ...result, onUpdate, onExpand, body };
}

const FOCUSABLE = 'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])';

describe('SpeakerClusterCard header', () => {
  it('is a native button that reports and toggles its expanded state', async () => {
    const { container, onExpand, body } = setup();
    const toggle = container.querySelector('button[aria-expanded]') as HTMLButtonElement;
    expect(toggle).not.toBeNull();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.hasAttribute('aria-controls')).toBe(false);

    await fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(body()?.id).toBe('cluster-body-c-1');
    expect(toggle.getAttribute('aria-controls')).toBe('cluster-body-c-1');
    expect(onExpand).toHaveBeenCalledTimes(1);
  });

  it('holds no focusable control inside the button', () => {
    const { container } = setup();
    const toggle = container.querySelector('[aria-expanded]') as HTMLElement;
    expect(toggle.querySelectorAll(FOCUSABLE)).toHaveLength(0);
  });

  it('does not toggle when a key other than Enter or Space passes over it', async () => {
    const { container, body } = setup();
    const toggle = container.querySelector('[aria-expanded]') as HTMLElement;
    // Checked after EACH key: the old handler toggled on every key, so an even number of
    // presses landed back on "collapsed".
    for (const key of ['Tab', 'ArrowDown', 'a']) {
      await fireEvent.keyDown(toggle, { key });
      expect(body()).toBeNull();
    }
  });

  it('typing into the label editor does not open or close the card', async () => {
    const { container, body } = setup();
    await fireEvent.dblClick(container.querySelector('.cluster-label') as HTMLElement);
    const input = container.querySelector('input.label-input') as HTMLInputElement;
    expect(input).not.toBeNull();
    for (const key of ['B', 'o', 'b', ' ']) {
      await fireEvent.keyDown(input, { key });
      expect(body()).toBeNull();
    }
  });

  it('opens the label editor from the keyboard and hands focus back when it closes', async () => {
    const { container, onUpdate } = setup();
    const edit = container.querySelector('.edit-label-btn') as HTMLButtonElement;
    expect(edit.tagName).toBe('BUTTON');
    expect(edit.textContent?.trim()).toBe('speakers.cluster.editLabelButton');
    // Outside the disclosure button: its own tab stop, not a child the reader would flatten.
    expect(edit.closest('[aria-expanded]')).toBeNull();

    await fireEvent.click(edit);
    const input = container.querySelector('input.label-input') as HTMLInputElement;
    expect(document.activeElement).toBe(input);
    await fireEvent.input(input, { target: { value: 'Bob' } });
    await fireEvent.keyDown(input, { key: 'Enter' });
    await tick();

    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onUpdate.mock.calls[0]?.[0].detail).toEqual({ uuid: 'c-1', label: 'Bob' });
    expect(document.activeElement).toBe(container.querySelector('.edit-label-btn'));
  });

  it('an edit ended by clicking elsewhere saves and leaves focus where the user put it', async () => {
    const { container, onUpdate } = setup();
    const elsewhere = document.body.appendChild(document.createElement('button'));
    await fireEvent.click(container.querySelector('.edit-label-btn') as HTMLElement);
    const input = container.querySelector('input.label-input') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'Bob' } });
    elsewhere.focus();
    await fireEvent.blur(input);
    await tick();

    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(elsewhere);
    elsewhere.remove();
  });

  it('Escape cancels the edit without saving', async () => {
    const { container, onUpdate } = setup();
    await fireEvent.click(container.querySelector('.edit-label-btn') as HTMLElement);
    const input = container.querySelector('input.label-input') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'Bob' } });
    await fireEvent.keyDown(input, { key: 'Escape' });
    await tick();
    expect(container.querySelector('input.label-input')).toBeNull();
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it('double-clicking the name edits it; double-clicking elsewhere on the row does not', async () => {
    const { container } = setup();
    await fireEvent.dblClick(container.querySelector('.member-count') as HTMLElement);
    expect(container.querySelector('input.label-input')).toBeNull();
    await fireEvent.dblClick(container.querySelector('.cluster-label') as HTMLElement);
    expect(container.querySelector('input.label-input')).not.toBeNull();
  });
});
