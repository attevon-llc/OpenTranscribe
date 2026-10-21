/**
 * "Run redaction" / "Not yet redacted" are offered only when the backend says redaction is
 * enabled for the viewer (`redaction_enabled`). With it off, a scan runs and succeeds but
 * masks nothing, which read as a button that silently did nothing.
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import RedactionControls from './RedactionControls.svelte';

describe('RedactionControls', () => {
  it('renders nothing for an owner whose redaction is not enabled', () => {
    const { container } = render(RedactionControls, {
      props: { canViewOriginal: true, redactionEnabled: false },
    });

    expect(container.querySelector('.redaction-footer')).toBeNull();
  });

  it('offers the run button for an owner when redaction is enabled', () => {
    const { getByText } = render(RedactionControls, {
      props: { canViewOriginal: true, redactionEnabled: true },
    });

    expect(getByText('settings.contentRedaction.notRedacted')).toBeTruthy();
    expect(getByText('settings.contentRedaction.runRedaction')).toBeTruthy();
  });

  it('hides the rescan link in the toggle row when redaction is not enabled', () => {
    const { queryByText } = render(RedactionControls, {
      props: { canViewOriginal: true, showRedactionToggle: true, redactionEnabled: false },
    });

    expect(queryByText('settings.contentRedaction.rescan')).toBeNull();
  });

  it('says the scan found nothing once detection is done but no spans are applied', () => {
    const { getByText, queryByText } = render(RedactionControls, {
      props: { canViewOriginal: true, redactionEnabled: true, redactionStatus: 'done' },
    });

    expect(getByText('settings.contentRedaction.scanNothingFound')).toBeTruthy();
    expect(queryByText('settings.contentRedaction.notRedacted')).toBeNull();
  });
});
