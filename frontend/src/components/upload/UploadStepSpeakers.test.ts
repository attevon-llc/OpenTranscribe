/**
 * `UploadStepSpeakers.svelte` is one of the few upload-wizard steps with real
 * client-side logic (see `frontend/src/components/upload/CLAUDE.md`): three
 * reactive clamps (`$: if (x !== null && x < 1) x = 1`, lower bound only —
 * there is no upper clamp in the markup or the script, just `min="1"` on the
 * `<input>`s) and a `hasValidationError` flag that fires only on the
 * `minSpeakers > maxSpeakers` case.
 *
 * `$t` is left unmocked (as in `RetrievalQualityNotice.test.ts` /
 * `SummaryActions.test.ts`): i18next is uninitialised here, so `$t('key')`
 * returns the raw key, which is what these tests assert on.
 */
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';

import UploadStepSpeakers from './UploadStepSpeakers.svelte';

function ids(container: HTMLElement) {
  return {
    min: container.querySelector('#min-speakers') as HTMLInputElement,
    max: container.querySelector('#max-speakers') as HTMLInputElement,
    num: container.querySelector('#num-speakers') as HTMLInputElement,
    error: container.querySelector('.validation-error'),
  };
}

describe('UploadStepSpeakers validation', () => {
  it('shows no validation error for a valid min <= max range', () => {
    const { container } = render(UploadStepSpeakers, {
      props: { minSpeakers: 2, maxSpeakers: 5, numSpeakers: null },
    });
    expect(ids(container).error).toBeNull();
  });

  it('flags a validation error when min > max', () => {
    const { container } = render(UploadStepSpeakers, {
      props: { minSpeakers: 5, maxSpeakers: 2, numSpeakers: null },
    });
    const { error } = ids(container);
    expect(error).not.toBeNull();
    expect(error?.textContent).toContain('uploader.minMaxValidationError');
  });

  it('does not flag an error when min === max', () => {
    const { container } = render(UploadStepSpeakers, {
      props: { minSpeakers: 3, maxSpeakers: 3, numSpeakers: null },
    });
    expect(ids(container).error).toBeNull();
  });

  it('clamps a sub-1 minSpeakers up to 1', () => {
    const { container } = render(UploadStepSpeakers, {
      props: { minSpeakers: 0, maxSpeakers: 5, numSpeakers: null },
    });
    expect(ids(container).min.value).toBe('1');
  });

  it('clamps a negative maxSpeakers up to 1', () => {
    const { container } = render(UploadStepSpeakers, {
      props: { minSpeakers: 1, maxSpeakers: -5, numSpeakers: null },
    });
    expect(ids(container).max.value).toBe('1');
  });

  it('clamps a sub-1 numSpeakers up to 1', () => {
    const { container } = render(UploadStepSpeakers, {
      props: { minSpeakers: null, maxSpeakers: null, numSpeakers: 0 },
    });
    expect(ids(container).num.value).toBe('1');
  });

  it('does not clamp a large minSpeakers/maxSpeakers — only a lower bound exists', () => {
    const { container } = render(UploadStepSpeakers, {
      props: { minSpeakers: 50, maxSpeakers: 100, numSpeakers: null },
    });
    const { min, max, error } = ids(container);
    expect(min.value).toBe('50');
    expect(max.value).toBe('100');
    expect(error).toBeNull();
  });

  it('leaves null values untouched (no clamp, no error, uses placeholder)', () => {
    const { container } = render(UploadStepSpeakers, {
      props: { minSpeakers: null, maxSpeakers: null, numSpeakers: null },
    });
    const { min, max, error } = ids(container);
    expect(min.value).toBe('');
    expect(max.value).toBe('');
    expect(error).toBeNull();
  });

  it('disables min/max inputs once a fixed numSpeakers is set', () => {
    const { container } = render(UploadStepSpeakers, {
      props: { minSpeakers: null, maxSpeakers: null, numSpeakers: 4 },
    });
    const { min, max } = ids(container);
    expect(min.disabled).toBe(true);
    expect(max.disabled).toBe(true);
  });

  it('leaves min/max inputs enabled when numSpeakers is null', () => {
    const { container } = render(UploadStepSpeakers, {
      props: { minSpeakers: 1, maxSpeakers: 2, numSpeakers: null },
    });
    const { min, max } = ids(container);
    expect(min.disabled).toBe(false);
    expect(max.disabled).toBe(false);
  });
});
