/**
 * The upload checkbox records the UPLOADER'S statement that the speakers agreed (issue #1356).
 * It is unchecked by default, bound to the parent's state, and absent while voiceprints are off
 * (there is nothing to consent to). Its wording must not claim the product verified anything.
 */
import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

import UploadStepSpeakers from './UploadStepSpeakers.svelte';
import en from '$lib/i18n/locales/en.json';

const box = (container: HTMLElement) =>
  container.querySelector('[data-testid="voiceprint-consent"]') as HTMLInputElement | null;

describe('UploadStepSpeakers voiceprint consent', () => {
  it('is unchecked by default', () => {
    const { container } = render(UploadStepSpeakers, { props: {} });
    expect(box(container)).not.toBeNull();
    expect(box(container)!.checked).toBe(false);
  });

  it('reflects and changes the bound state', async () => {
    const { container } = render(UploadStepSpeakers, { props: { voiceprintConsent: true } });
    expect(box(container)!.checked).toBe(true);
    await fireEvent.click(box(container)!);
    expect(box(container)!.checked).toBe(false);
  });

  it('is not offered while voiceprints are off', () => {
    const { container } = render(UploadStepSpeakers, { props: { voiceprintsOn: false } });
    expect(box(container)).toBeNull();
  });
});

describe('voiceprint consent wording', () => {
  it('says it records a statement and that nothing is verified', () => {
    const text = [
      en['uploader.voiceprintConsent.label'],
      en['uploader.voiceprintConsent.hint'],
    ].join(' ');
    expect(text).toMatch(/I state/);
    expect(text).toMatch(/does not verify/);
  });

  it('never claims consent was obtained or confirmed', () => {
    const all = Object.entries(en)
      .filter(([k]) => /voiceprintConsent/.test(k))
      .map(([, v]) => v as string)
      .join('\n');
    expect(all.length).toBeGreaterThan(100);
    expect(all).not.toMatch(/consent (was )?(obtained|confirmed|verified)|verified consent/i);
  });
});
