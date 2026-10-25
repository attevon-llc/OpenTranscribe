import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';
import type { BuildIdentity } from '$lib/utils/buildIdentity';

/**
 * The stale-build banner and the source link must follow the build-identity rules (issue
 * #1273). A server running a `dev-<sha>` image can never equal the bundle's release semver, so
 * comparing versions alone showed "the app build doesn't match the server" on every dev image
 * even for a fresh tab, and the source offer for such a server pointed at `/releases/tag/vdev-<sha>`.
 *
 * The client identity is a `define` constant, which is inlined at compile time and cannot be
 * stubbed, so the component reads it through `$lib/utils/clientBuild`, which this test replaces.
 */

const REPO = 'https://github.com/attevon-llc/OpenTranscribe';

const client = vi.hoisted(() => ({
  build: { version: '0.5.1', commit: 'unknown' } as { version: string; commit: string },
}));
vi.mock('$lib/utils/clientBuild', () => ({
  get CLIENT_BUILD() {
    return client.build;
  },
}));

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

function serverReports(version: string, gitSha: string) {
  axiosGet.mockResolvedValue({ data: { version, git_sha: gitSha, build_time: '' } });
}

async function openModal() {
  render(AboutModal, { props: { showModal: true } } as never);
  await waitFor(() => expect(axiosGet).toHaveBeenCalledWith('/api/version'));
  // The server version appears in the header once the probe has resolved.
  await waitFor(() => expect(document.querySelectorAll('.header-version-number').length).toBe(2));
}

function sourceOfferHref(): string | undefined {
  return Array.from(document.querySelectorAll<HTMLAnchorElement>('.legal-section a')).find(
    (a) => a.href.includes('/tree/') || a.href.includes('/releases/tag/')
  )?.href;
}

const banner = () => document.querySelector('.version-mismatch');
const serverLabel = () => document.querySelectorAll('.header-version-number')[1];

function clientIs(identity: BuildIdentity & { version: string; commit: string }) {
  client.build = identity;
}

beforeEach(() => {
  axiosGet.mockReset();
  clientIs({ version: '0.5.1', commit: 'unknown' });
});

describe('AboutModal stale-build banner (issue #1273)', () => {
  it('shows no banner when the server is a dev-<sha> image and the bundle knows no commit', async () => {
    serverReports('dev-abc1234', 'abc1234');

    await openModal();

    expect(banner()).toBeNull();
    expect(serverLabel()).not.toHaveClass('mismatch');
  });

  it('shows no banner when the bundle and a dev image come from the same commit', async () => {
    clientIs({ version: '0.5.1', commit: 'abc1234' });
    serverReports('dev-abc1234', 'abc1234');

    await openModal();

    expect(banner()).toBeNull();
  });

  it('shows no banner for the same commit under a different release tag', async () => {
    clientIs({ version: '0.6.0', commit: 'abc1234' });
    serverReports('v0.6.1', 'abc1234');

    await openModal();

    expect(banner()).toBeNull();
  });

  it('shows the banner when the server dev image is a different commit than the bundle', async () => {
    clientIs({ version: '0.5.1', commit: 'abc1234' });
    serverReports('dev-def5678', 'def5678');

    await openModal();

    expect(banner()).not.toBeNull();
    expect(screen.getByText('about.versionMismatch')).toBeInTheDocument();
    expect(serverLabel()).toHaveClass('mismatch');
  });

  it('control: still shows the banner for a real release mismatch', async () => {
    serverReports('v9.9.9', 'unknown');

    await openModal();

    expect(banner()).not.toBeNull();
    expect(serverLabel()).toHaveClass('mismatch');
  });

  it('control: a matching release with and without the v prefix shows no banner', async () => {
    serverReports('v0.5.1', 'unknown');

    await openModal();

    expect(banner()).toBeNull();
  });

  it('stays silent when the server cannot identify itself', async () => {
    serverReports('unknown', 'unknown');

    await openModal();

    expect(banner()).toBeNull();
  });
});

describe('AboutModal client build label', () => {
  it('appends the commit when the bundle knows it, in an LTR island', async () => {
    clientIs({ version: '0.5.1', commit: 'abc1234' });
    serverReports('v0.5.1', 'abc1234');

    await openModal();

    const label = document.querySelectorAll('.header-version-number')[0];
    expect(label.textContent).toContain('0.5.1 (abc1234)');
    // Parentheses must not mirror in the RTL (ar) UI.
    expect(label.querySelector('bdi')).toHaveAttribute('dir', 'ltr');
  });

  it('shows the bare version when the bundle recorded no commit', async () => {
    serverReports('v0.5.1', 'abc1234');

    await openModal();

    const label = document.querySelectorAll('.header-version-number')[0];
    expect(label.textContent).toContain('0.5.1');
    expect(label.textContent).not.toContain('(');
  });
});

describe('AboutModal source offer for dev images (issue #1273)', () => {
  it('links /tree/<sha>, never /releases/tag/vdev-<sha>, when git_sha is unknown', async () => {
    serverReports('dev-abc1234', 'unknown');

    await openModal();

    expect(sourceOfferHref()).toBe(`${REPO}/tree/abc1234`);
    expect(screen.getByText('about.legal.viewSourceExact')).toBeInTheDocument();
  });

  it('does not claim an exact source for a dirty tree, and drops the -dirty suffix from the URL', async () => {
    serverReports('dev-abc1234', 'abc1234-dirty');

    await openModal();

    expect(sourceOfferHref()).toBe(`${REPO}/tree/abc1234`);
    expect(screen.queryByText('about.legal.viewSourceExact')).not.toBeInTheDocument();
    expect(screen.getByText('about.legal.sourceOfferUnidentified')).toBeInTheDocument();
  });
});
