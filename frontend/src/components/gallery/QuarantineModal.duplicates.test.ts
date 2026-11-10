import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
vi.mock('$lib/api/admin', () => ({ AdminApi: { quarantineFile: vi.fn() } }));
vi.mock('$lib/api/takedownNotices', () => ({
  TakedownNoticesApi: { list: vi.fn().mockResolvedValue({ notices: [], total: 0 }) },
}));

import QuarantineModal from './QuarantineModal.svelte';
import type { MediaFile } from '$lib/types/media';

const file = (uuid: string) =>
  ({
    uuid,
    filename: 'recording.m4a',
    status: 'completed',
    upload_time: '2026-01-01T00:00:00Z',
  }) as MediaFile;

describe('QuarantineModal with files that share a name', () => {
  it('lists every selected file even when two have the same title', () => {
    render(QuarantineModal, { props: { isOpen: true, files: [file('a'), file('b')] } });
    expect(document.querySelectorAll('.file-list li')).toHaveLength(2);
  });
});
