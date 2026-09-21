/**
 * `GenderBadge` — icon + translated word for a predicted gender (issue #756).
 *
 * The defect this exists to prevent: `SpeakerClusterCard`, `ClusterMemberList` (x2, before
 * being consolidated here), and `SpeakerInboxItem` each independently rendered
 * `{#if gender === 'male'}…{:else}…female…{/if}` with NO guard excluding null/unknown —
 * so a speaker with no prediction at all, or a future third value, rendered a confidently
 * wrong "Female" label instead of nothing. `SpeakerEditorPanel`'s existing (correct) pattern
 * is safe only because it guards the outer `{#if}` on `predicted_gender && !== 'unknown'`
 * first; this component bakes that same guard in once, for every consumer.
 */
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import GenderBadge from './GenderBadge.svelte';

describe('GenderBadge', () => {
  it('renders the male icon and word', () => {
    const { container } = render(GenderBadge, { props: { gender: 'male' } });
    expect(container.querySelector('.gender-icon')).not.toBeNull();
    expect(container.textContent).toContain('speakers.member.male');
  });

  it('renders the female icon and word', () => {
    const { container } = render(GenderBadge, { props: { gender: 'female' } });
    expect(container.querySelector('.gender-icon')).not.toBeNull();
    expect(container.textContent).toContain('speakers.member.female');
  });

  it('renders NOTHING for null — does not default to female', () => {
    const { container } = render(GenderBadge, { props: { gender: null } });
    expect(container.querySelector('.gender-icon')).toBeNull();
    expect(container.textContent).toBe('');
  });

  it('renders NOTHING for undefined', () => {
    const { container } = render(GenderBadge, { props: {} });
    expect(container.querySelector('.gender-icon')).toBeNull();
  });

  it('renders NOTHING for an unrecognised value ("unknown", or any future third value) — does not default to female', () => {
    const unknown = render(GenderBadge, { props: { gender: 'unknown' } });
    expect(unknown.container.querySelector('.gender-icon')).toBeNull();

    const empty = render(GenderBadge, { props: { gender: '' } });
    expect(empty.container.querySelector('.gender-icon')).toBeNull();

    const future = render(GenderBadge, { props: { gender: 'nonbinary' } });
    expect(future.container.querySelector('.gender-icon')).toBeNull();
  });

  it('shows the confidence percentage in the tooltip when provided', () => {
    const { container } = render(GenderBadge, { props: { gender: 'male', confidence: 0.87 } });
    const badge = container.querySelector('.gender-icon');
    expect(badge?.getAttribute('title')).toContain('87%');
  });

  it('omits the confidence percentage from the tooltip when absent', () => {
    const { container } = render(GenderBadge, { props: { gender: 'male' } });
    const badge = container.querySelector('.gender-icon');
    expect(badge?.getAttribute('title')).not.toContain('%');
  });

  it('shows the confirmed tick only when confirmed is true', () => {
    const confirmed = render(GenderBadge, { props: { gender: 'male', confirmed: true } });
    expect(confirmed.container.querySelector('.gender-confirmed-tick')).not.toBeNull();

    const unconfirmed = render(GenderBadge, { props: { gender: 'male', confirmed: false } });
    expect(unconfirmed.container.querySelector('.gender-confirmed-tick')).toBeNull();
  });
});
