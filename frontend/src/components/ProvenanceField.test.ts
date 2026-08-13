/**
 * The provenance badge is the UI half of "never report a derived value as a fact".
 *
 * The backend refuses to store a date without a source; this component is what stops the
 * same value being *rendered* without one. So the tests that matter here are not "it shows
 * a date" — they are the ones that would fail if the badge were quietly dropped to tidy the
 * layout, if a conflict were hidden, or if an unresolved file were presented as if it had an
 * answer. Each has a paired negative control so it cannot pass by rendering nothing.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      // The key IS the rendered text, so an assertion names the key rather than a
      // translation that could change without the component changing.
      run((key: string) => key);
      return () => {};
    },
  },
}));

import ProvenanceField from './ProvenanceField.svelte';

const FILENAME_PROVENANCE = {
  source: 'filename' as const,
  confidence: 0.85,
  locked: false,
  conflict: false,
  candidates: [
    {
      source: 'filename' as const,
      date: '2024-03-15T00:00:00Z',
      evidence: "filename: '2024-03-15'",
    },
  ],
};

function renderField(props: Record<string, unknown> = {}) {
  const onSave = vi.fn();
  render(ProvenanceField, {
    props: { label: 'Recorded', ...props },
    events: { save: onSave },
  } as never);
  return { onSave };
}

describe('the value never appears without where it came from', () => {
  it('renders the source badge beside a derived value', () => {
    renderField({ value: '2024-03-15T00:00:00Z', provenance: FILENAME_PROVENANCE });
    expect(screen.getByText('provenance.source.filename')).toBeTruthy();
  });

  it('marks an unresolved file as not yet determined rather than showing a bare blank', () => {
    // The negative control for the badge test, and a real distinction: a null provenance
    // means the resolver has not run, which is NOT the same as "no source could date this".
    renderField({ value: null, provenance: null });
    expect(screen.getByText('provenance.noneRecorded')).toBeTruthy();
    expect(screen.getByText('provenance.source.unresolved')).toBeTruthy();
  });

  it('shows a manual value as user-set, not as an inference', () => {
    renderField({
      value: '2024-03-15T00:00:00Z',
      provenance: { ...FILENAME_PROVENANCE, source: 'manual', locked: true },
    });
    expect(screen.getByText('provenance.source.manual')).toBeTruthy();
  });
});

describe('a disagreement between sources is surfaced, not resolved silently', () => {
  // The winner is `container`, because container outranks filename in the resolver's
  // precedence — so this mirrors a real disagreement rather than an invented one.
  const CONFLICTED = {
    ...FILENAME_PROVENANCE,
    source: 'container' as const,
    conflict: true,
    candidates: [
      {
        source: 'container' as const,
        date: '2024-03-14T09:04:00Z',
        evidence: 'container metadata',
      },
      {
        source: 'filename' as const,
        date: '2024-03-15T00:00:00Z',
        evidence: "filename: '2024-03-15'",
      },
    ],
  };

  it('explains the conflict and lists every candidate when the badge is opened', async () => {
    renderField({ value: '2024-03-14T09:04:00Z', provenance: CONFLICTED });

    await fireEvent.click(screen.getByText(/provenance.source.container/));
    expect(screen.getByText('provenance.conflictNote')).toBeTruthy();
    // BOTH sources, including the one that lost. Showing only the winner would make the
    // disagreement invisible, which is the same as not detecting it.
    expect(screen.getAllByText('provenance.source.filename').length).toBeGreaterThan(0);
  });

  it('does not show a conflict note when the sources agree', () => {
    // Without this the note could be rendered unconditionally and the test above would
    // still pass — a warning that always fires is one the UI learns to ignore.
    renderField({ value: '2024-03-15T00:00:00Z', provenance: FILENAME_PROVENANCE });
    expect(screen.queryByText('provenance.conflictNote')).toBeNull();
  });
});

describe('the user can correct the value', () => {
  it('emits the edited date as an ISO instant', async () => {
    const { onSave } = renderField({
      value: '2024-03-15T00:00:00Z',
      provenance: FILENAME_PROVENANCE,
    });

    await fireEvent.click(screen.getByText('provenance.edit'));
    const input = screen.getByLabelText('Recorded') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: '2019-06-02' } });
    await fireEvent.click(screen.getByText('provenance.save'));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0].detail.value).toBe('2019-06-02T00:00:00.000Z');
  });

  it('emits null when the field is cleared, so a mistaken edit is retractable', async () => {
    // A cleared field must UNLOCK the row rather than lock it at nothing — otherwise a
    // user who set a date by accident has disabled automatic resolution forever with no
    // way back and no indication that is what happened.
    const { onSave } = renderField({
      value: '2024-03-15T00:00:00Z',
      provenance: FILENAME_PROVENANCE,
    });

    await fireEvent.click(screen.getByText('provenance.edit'));
    const input = screen.getByLabelText('Recorded') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: '' } });
    await fireEvent.click(screen.getByText('provenance.save'));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0].detail.value).toBeNull();
  });

  it('emits nothing when the edit is cancelled', async () => {
    const { onSave } = renderField({
      value: '2024-03-15T00:00:00Z',
      provenance: FILENAME_PROVENANCE,
    });

    await fireEvent.click(screen.getByText('provenance.edit'));
    await fireEvent.click(screen.getByText('provenance.cancel'));

    expect(onSave).not.toHaveBeenCalled();
  });

  it('offers no edit control on a read-only surface', () => {
    renderField({
      value: '2024-03-15T00:00:00Z',
      provenance: FILENAME_PROVENANCE,
      editable: false,
    });
    expect(screen.queryByText('provenance.edit')).toBeNull();
    // ...but the badge is still there. Read-only means "cannot change it", never
    // "shown without its origin".
    expect(screen.getByText('provenance.source.filename')).toBeTruthy();
  });
});
