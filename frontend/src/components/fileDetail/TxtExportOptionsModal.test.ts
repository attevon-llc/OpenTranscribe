import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import TxtExportOptionsModal from './TxtExportOptionsModal.svelte';

describe('TxtExportOptionsModal', () => {
  it('hides the decorative redaction icon from assistive technology', () => {
    render(TxtExportOptionsModal, { props: { show: true, redactionActive: true } });

    const icon = document.querySelector('.export-redaction-icon');
    expect(icon).not.toBeNull();
    expect(icon?.getAttribute('aria-hidden')).toBe('true');
  });

  it('disables the speaker option with a hint when diarization is off', () => {
    render(TxtExportOptionsModal, { props: { show: true, diarizationDisabled: true } });

    const speakers = screen.getByLabelText(/exportOptions\.includeSpeakers/) as HTMLInputElement;
    expect(speakers.disabled).toBe(true);
    expect(screen.getByText('exportOptions.diarizationDisabledHint')).toBeTruthy();
  });

  it('forwards confirm', async () => {
    const onConfirm = vi.fn();
    render(TxtExportOptionsModal, {
      props: { show: true },
      events: { confirm: onConfirm },
    } as never);

    await fireEvent.click(screen.getByRole('button', { name: /exportOptions\.export/ }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
