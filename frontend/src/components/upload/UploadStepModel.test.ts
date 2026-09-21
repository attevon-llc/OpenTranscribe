/**
 * `UploadStepModel.svelte` had no test at all before #751, despite carrying the
 * one behaviour change in that issue with a real regression risk: the AI-summary
 * toggle's display polarity (issue plan §7.3). The component keeps `skipSummary`
 * as its wire-compatible prop (`false` = a summary IS generated) but presents a
 * positive `generateSummary` checkbox derived from it, so the checked state must
 * always be the OPPOSITE of `skipSummary`, and toggling it must round-trip.
 *
 * No `component.$on('change', …)` spy here: this codebase has no precedent for
 * asserting on a Svelte `createEventDispatcher` payload in a test (verified —
 * zero call sites), and Svelte 5's compiled component type has no callable
 * `$on` for a function component, so it doesn't type-check under `npm run
 * check` either. The round-trip is instead observed through the DOM: firing
 * `change` flips the internal `skipSummary`, which re-derives `generateSummary`,
 * which re-renders the checkbox's `checked` attribute — so asserting the
 * checkbox settles back to the state that implies the correct `skipSummary`
 * is an equivalent, DOM-only observation of the same behaviour.
 *
 * `$t` is left unmocked (as in `UploadStepSpeakers.test.ts`): i18next is
 * uninitialised here, so `$t('key')` returns the raw key, which is what these
 * tests assert on.
 */
import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

import UploadStepModel from './UploadStepModel.svelte';

function summaryCheckbox(container: HTMLElement) {
  return container.querySelector('.toggle-switch input[type="checkbox"]') as HTMLInputElement;
}

describe('UploadStepModel — AI summary toggle polarity (#751 item 3)', () => {
  it('defaults to CHECKED (summary generated) when skipSummary is false', () => {
    const { container } = render(UploadStepModel, { props: { skipSummary: false } });
    expect(summaryCheckbox(container).checked).toBe(true);
  });

  it('renders UNCHECKED when skipSummary is true', () => {
    const { container } = render(UploadStepModel, { props: { skipSummary: true } });
    expect(summaryCheckbox(container).checked).toBe(false);
  });

  it('unchecking the positive toggle settles back to unchecked (skipSummary flips to true)', async () => {
    const { container } = render(UploadStepModel, { props: { skipSummary: false } });
    const checkbox = summaryCheckbox(container);
    expect(checkbox.checked).toBe(true);

    checkbox.checked = false;
    await fireEvent.change(checkbox);

    // If the handler had NOT flipped `skipSummary` (the naive `bind:checked`
    // regression), the derived `generateSummary` would still be `true` and
    // Svelte would snap the checkbox back to checked on the next render.
    expect(checkbox.checked).toBe(false);
  });

  it('checking the positive toggle settles back to checked (skipSummary flips to false)', async () => {
    const { container } = render(UploadStepModel, { props: { skipSummary: true } });
    const checkbox = summaryCheckbox(container);
    expect(checkbox.checked).toBe(false);

    checkbox.checked = true;
    await fireEvent.change(checkbox);

    expect(checkbox.checked).toBe(true);
  });

  it('the toggle label uses the positive copy key, not the retired negative one', () => {
    const { container } = render(UploadStepModel, { props: { skipSummary: false } });
    expect(container.textContent).toContain('upload.generateSummary');
    expect(container.textContent).toContain('upload.generateSummaryHint');
    expect(container.textContent).not.toContain('upload.skipSummary');
  });

  it('appends the quality/speed suffix keys to each model option (#751 item 2)', () => {
    const { container } = render(UploadStepModel, {
      props: { adminDefaultModel: 'large-v3-turbo' },
    });
    const options = Array.from(container.querySelectorAll('option')).map((o) => o.textContent);
    expect(options.some((t) => t?.includes('uploader.highQualitySuffix'))).toBe(true);
    expect(options.some((t) => t?.includes('uploader.fastProcessingSuffix'))).toBe(true);
  });
});
