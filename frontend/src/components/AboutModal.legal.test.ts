import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

/**
 * The AGPL §13 source offer, and the version probe it depends on (issue #862).
 *
 * §13 requires a modified version made available over a network to offer its users the
 * Corresponding Source. A link discharges that only if it resolves to the code that is
 * RUNNING — so the modal reads `git_sha` from `/api/version` (`/health` does not carry it)
 * and degrades explicitly rather than silently pointing at whatever `master` is today.
 */

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const axiosGet = vi.fn();
vi.mock('axios', () => ({ default: { get: (...args: unknown[]) => axiosGet(...args) } }));

import AboutModal from './AboutModal.svelte';

const REPO = 'https://github.com/attevon-llc/OpenTranscribe';

function legalLinks(): HTMLAnchorElement[] {
  return Array.from(document.querySelectorAll<HTMLAnchorElement>('.legal-section a'));
}

function sourceOfferHref(): string | undefined {
  return legalLinks().find((a) => a.href.includes('/tree/') || a.href.includes('/releases/tag/'))
    ?.href;
}

beforeEach(() => {
  axiosGet.mockReset();
  axiosGet.mockResolvedValue({ data: { version: 'v0.5.0', git_sha: 'abc1234', build_time: '' } });
});

describe('AboutModal legal section (issue #862)', () => {
  it('reads the build identity from /api/version, not /health', async () => {
    render(AboutModal, { props: { showModal: true } } as never);

    // /health has no `git_sha`, so the source offer could not resolve to the running
    // commit at all — the endpoint change IS the fix, not an incidental cleanup.
    await waitFor(() => expect(axiosGet).toHaveBeenCalledWith('/api/version'));
  });

  it('names the licence and links to the shipped LICENSE file', () => {
    render(AboutModal, { props: { showModal: true } } as never);

    expect(screen.getByText('about.legal.licenseName')).toBeInTheDocument();
    expect(legalLinks().map((a) => a.href)).toContain(`${REPO}/blob/master/LICENSE`);
  });

  it('links the third-party attributions (NOTICE) as well as the licence', () => {
    render(AboutModal, { props: { showModal: true } } as never);

    expect(legalLinks().map((a) => a.href)).toContain(`${REPO}/blob/master/NOTICE`);
  });

  it('offers the source at the EXACT commit when the build recorded one', async () => {
    render(AboutModal, { props: { showModal: true } } as never);

    await waitFor(() => expect(sourceOfferHref()).toBe(`${REPO}/tree/abc1234`));
    expect(screen.getByText('about.legal.viewSourceExact')).toBeInTheDocument();
    expect(screen.queryByText('about.legal.sourceOfferUnidentified')).not.toBeInTheDocument();
  });

  it('falls back to the release tag when the commit is unknown', async () => {
    axiosGet.mockResolvedValue({ data: { version: 'v0.5.0', git_sha: 'unknown' } });

    render(AboutModal, { props: { showModal: true } } as never);

    await waitFor(() => expect(sourceOfferHref()).toBe(`${REPO}/releases/tag/v0.5.0`));
    expect(screen.queryByText('about.legal.sourceOfferUnidentified')).not.toBeInTheDocument();
  });

  it('SAYS the build could not identify itself rather than implying an exact link', async () => {
    axiosGet.mockResolvedValue({ data: { version: 'unknown', git_sha: 'unknown' } });

    render(AboutModal, { props: { showModal: true } } as never);

    await waitFor(() =>
      expect(screen.getByText('about.legal.sourceOfferUnidentified')).toBeInTheDocument()
    );
    const repoLink = legalLinks().find((a) => a.href === REPO);
    expect(repoLink, 'the offer must still resolve somewhere').toBeDefined();
  });

  it('still offers the source when the version probe fails outright', async () => {
    axiosGet.mockRejectedValue(new Error('network down'));

    render(AboutModal, { props: { showModal: true } } as never);

    // An unreachable backend is not a licence exemption: the offer degrades to the
    // repository root and says so, rather than disappearing.
    await waitFor(() =>
      expect(screen.getByText('about.legal.sourceOfferUnidentified')).toBeInTheDocument()
    );
  });

  it('says who owes the source when the deployment has been modified', () => {
    render(AboutModal, { props: { showModal: true } } as never);

    // The link names the UPSTREAM repository. A downstream operator who modified this
    // owes their users THEIR source; until the URL is configurable, the UI must say so
    // instead of quietly offering code that is not running.
    expect(screen.getByText('about.legal.sourceOfferModified')).toBeInTheDocument();
  });
});
