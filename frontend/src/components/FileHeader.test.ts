/**
 * Issue #841/#786 — the file-detail error panel must render the backend's sanitized
 * `user_message`/`error_suggestions`/`is_retryable`, never a raw exception string, and the
 * dead `error_message` field (#841's own defect: a backend field with no frontend consumer,
 * and a frontend field the API never actually sent) must not silently keep working.
 *
 * No test file existed for this component before — see `frontend/src/components/CLAUDE.md`
 * and the root `$t` mocking pattern used across this suite (e.g. `VideoPlayer.test.ts`).
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import type { MediaFileDetail } from '$lib/types/media';

vi.mock('$lib/axios', () => ({ default: { put: vi.fn() }, isRequestCancelled: () => false }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import FileHeader from './FileHeader.svelte';

function baseFile(overrides: Partial<MediaFileDetail> = {}): MediaFileDetail {
  return {
    uuid: 'file-uuid-1',
    filename: 'meeting.mp4',
    status: 'completed',
    upload_time: '2026-01-01T00:00:00Z',
    ...overrides,
  } as MediaFileDetail;
}

describe('FileHeader — error panel', () => {
  it('renders the backend user_message when the file errored', () => {
    const file = baseFile({
      status: 'error',
      user_message: 'This file could not be read. It may be corrupted.',
    });

    const { getByText, container } = render(FileHeader, { props: { file } });

    expect(container.querySelector('.status-message.error')).not.toBeNull();
    expect(getByText('This file could not be read. It may be corrupted.')).toBeTruthy();
  });

  it('renders each backend suggestion', () => {
    const file = baseFile({
      status: 'error',
      user_message: "This file's format or codec is not supported.",
      error_suggestions: [
        'Convert to a supported format',
        'Try re-encoding with standard settings',
      ],
    });

    const { getByText, container } = render(FileHeader, { props: { file } });

    const list = container.querySelector('.error-suggestions');
    expect(list).not.toBeNull();
    expect(getByText('Convert to a supported format')).toBeTruthy();
    expect(getByText('Try re-encoding with standard settings')).toBeTruthy();
  });

  it('shows the retryable hint only when the backend marks the error retryable', () => {
    const retryable = baseFile({ status: 'error', user_message: 'x', is_retryable: true });
    const notRetryable = baseFile({ status: 'error', user_message: 'x', is_retryable: false });

    const { container: withRetry } = render(FileHeader, { props: { file: retryable } });
    expect(withRetry.querySelector('.error-retryable')).not.toBeNull();

    const { container: withoutRetry } = render(FileHeader, { props: { file: notRetryable } });
    expect(withoutRetry.querySelector('.error-retryable')).toBeNull();
  });

  it('renders nothing for a legacy error_message-only object — the dead field is gone', () => {
    // Pins that #841's removed field cannot silently start "working" again: an object
    // shaped like the OLD wire contract (no `user_message`) must fall back to the generic
    // copy, not surface the stale field even if something upstream still sets it.
    const legacyShapedFile = baseFile({
      status: 'error',
      ...({ error_message: 'RAW: /srv/internal/whatever failed' } as Record<string, unknown>),
    });

    const { getByText, queryByText, container } = render(FileHeader, {
      props: { file: legacyShapedFile },
    });

    expect(container.querySelector('.status-message.error')).not.toBeNull();
    expect(queryByText('RAW: /srv/internal/whatever failed')).toBeNull();
    // Falls back to the i18n key itself under the mocked $t.
    expect(getByText('fileDetail.errorGeneric')).toBeTruthy();
  });

  it('renders no error panel for a completed file', () => {
    const file = baseFile({ status: 'completed' });

    const { container } = render(FileHeader, { props: { file } });

    expect(container.querySelector('.status-message.error')).toBeNull();
  });
});
