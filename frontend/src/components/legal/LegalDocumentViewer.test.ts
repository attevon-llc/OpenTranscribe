import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, string>) => string) => void) => {
      run((key: string, vars?: Record<string, string>) =>
        vars ? `${key}:${Object.values(vars).join(',')}` : key
      );
      return () => {};
    },
  },
}));

import LegalDocumentViewer from './LegalDocumentViewer.svelte';

const doc = {
  key: 'opentranscribe-terms-of-use',
  title: 'Terms of Use',
  version: '2.1',
  status: 'draft',
  sha256: '0123456789abcdef0123456789abcdef',
  markdown:
    '# Heading\n\nSafe paragraph.\n\n<script>window.__pwned = true</script>\n\n<img src=x onerror="window.__pwned = true">',
};

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockResolvedValue({ data: doc });
});

describe('LegalDocumentViewer', () => {
  it('renders sanitized markdown: scripts and handlers never reach the DOM', async () => {
    render(LegalDocumentViewer, { isOpen: true, docKey: doc.key });

    const prose = await screen.findByTestId('legal-prose');
    // The sanitizer module is imported lazily, so the html lands after the document does.
    await waitFor(() => expect(prose.textContent).toContain('Safe paragraph.'));
    expect(prose.querySelector('script')).toBeNull();
    expect(prose.querySelector('img')).toBeNull();
    expect(prose.innerHTML).not.toContain('onerror');
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
    expect(mockAxios.get).toHaveBeenCalledWith('/legal/documents/opentranscribe-terms-of-use');
  });

  it('shows the draft banner, version and a truncated fingerprint', async () => {
    render(LegalDocumentViewer, { isOpen: true, docKey: doc.key });

    expect(await screen.findByTestId('legal-draft-banner')).toBeTruthy();
    const fp = screen.getByTestId('legal-fingerprint');
    expect(fp.textContent).toContain('0123456789ab');
    expect(fp.textContent).not.toContain(doc.sha256);
    expect(fp.getAttribute('title')).toBe(doc.sha256);
    expect(screen.getByText('legal.viewer.version:2.1')).toBeTruthy();
  });

  it('omits the draft banner for a non-draft document', async () => {
    mockAxios.get.mockResolvedValue({ data: { ...doc, status: 'final' } });
    render(LegalDocumentViewer, { isOpen: true, docKey: doc.key });

    await screen.findByTestId('legal-prose');
    expect(screen.queryByTestId('legal-draft-banner')).toBeNull();
  });

  it('switches between the dialog and full screen from a labelled header control', async () => {
    render(LegalDocumentViewer, { isOpen: true, docKey: doc.key });
    await screen.findByTestId('legal-prose');

    const dialog = screen.getByRole('dialog');
    const toggle = screen.getByTestId('legal-fullscreen-toggle');
    expect(dialog.classList.contains('fullscreen')).toBe(false);
    expect(toggle.textContent).toContain('legal.viewer.enterFullscreen');

    await fireEvent.click(toggle);
    expect(dialog.classList.contains('fullscreen')).toBe(true);
    expect(toggle.textContent).toContain('legal.viewer.exitFullscreen');

    await fireEvent.click(toggle);
    expect(dialog.classList.contains('fullscreen')).toBe(false);
  });

  it('opens straight into full screen when the caller asks for it', async () => {
    render(LegalDocumentViewer, { isOpen: true, docKey: doc.key, startFullscreen: true });
    await screen.findByTestId('legal-prose');

    expect(screen.getByRole('dialog').classList.contains('fullscreen')).toBe(true);
  });
});
