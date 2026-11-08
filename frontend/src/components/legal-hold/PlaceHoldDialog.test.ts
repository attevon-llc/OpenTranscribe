/**
 * The one "Place legal hold" dialog (legal-hold plan, Lane D): validation the server would
 * apply, the organization or operator API by who is placing it, the 500-file cap, and the
 * server's answer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).englishLocale()
);
const orgApi = vi.hoisted(() => ({ placeHold: vi.fn() }));
vi.mock('$lib/api/orgAdmin', () => ({ OrgAdminApi: orgApi }));
const adminApi = vi.hoisted(() => ({ placeHold: vi.fn() }));
vi.mock('$lib/api/adminHolds', () => ({ AdminHoldsApi: adminApi }));
const collectionsApi = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock('$lib/api/collections', () => ({ CollectionsApi: collectionsApi }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import PlaceHoldDialog from './PlaceHoldDialog.svelte';

const F1 = '11111111-1111-1111-1111-111111111111';
const F2 = '22222222-2222-2222-2222-222222222222';
const REASON = 'Preservation notice, matter 7';

beforeEach(() => {
  vi.clearAllMocks();
  collectionsApi.list.mockResolvedValue([]);
  orgApi.placeHold.mockResolvedValue({ uuid: 'h', file_count: 2 });
  adminApi.placeHold.mockResolvedValue({ uuid: 'h', file_count: 2 });
});

function open(
  props: Record<string, unknown>,
  events: Record<string, (e: CustomEvent) => void> = {}
) {
  return render(PlaceHoldDialog, {
    props: { isOpen: true, fileUuids: [F1, F2], ...props },
    events,
  } as never);
}

const reasonBox = () => screen.getByTestId('place-hold-reason');
const submit = () => screen.getByRole('button', { name: 'Place hold' });

describe('PlaceHoldDialog', () => {
  it('keeps Place disabled until the reason is 10 to 2,000 characters, with a live count', async () => {
    open({ placer: 'platform' });
    expect(screen.getByText('Files to hold: 2')).toBeInTheDocument();
    expect(submit()).toBeDisabled();

    await fireEvent.input(reasonBox(), { target: { value: 'too short' } });
    expect(screen.getByText('9 / 2000')).toBeInTheDocument();
    expect(submit()).toBeDisabled();

    await fireEvent.input(reasonBox(), { target: { value: `  ${REASON}  ` } });
    expect(screen.getByText(`${REASON.length} / 2000`)).toBeInTheDocument();
    expect(submit()).toBeEnabled();
  });

  it('keeps the reason count "used / max" left to right in right-to-left languages', async () => {
    // Inheriting RTL showed "2000 / 55" in Arabic; the count is a technical value.
    open({ placer: 'platform' });
    await fireEvent.input(reasonBox(), { target: { value: REASON } });
    expect(screen.getByText(`${REASON.length} / 2000`).getAttribute('dir')).toBe('ltr');
  });

  it('places an operator hold through /admin/holds for an instance admin', async () => {
    const placed = vi.fn();
    open({ placer: 'platform' }, { placed: (e) => placed(e.detail) });
    await fireEvent.input(reasonBox(), { target: { value: REASON } });
    await fireEvent.click(submit());

    await waitFor(() =>
      expect(adminApi.placeHold).toHaveBeenCalledWith({
        scope: 'file',
        file_uuids: [F1, F2],
        reason: REASON,
        matter_ref: undefined,
      })
    );
    expect(orgApi.placeHold).not.toHaveBeenCalled();
    expect(toast.success).toHaveBeenCalledWith('Legal hold placed on 2 files.');
    expect(placed).toHaveBeenCalledWith(expect.objectContaining({ fileUuids: [F1, F2] }));
  });

  it('places an organization hold through /org-admin/holds for an org admin, with the matter', async () => {
    open({ placer: 'organization' });
    await fireEvent.input(reasonBox(), { target: { value: REASON } });
    await fireEvent.input(screen.getByLabelText('Matter reference (optional)'), {
      target: { value: ' CASE-9 ' },
    });
    await fireEvent.click(submit());

    await waitFor(() =>
      expect(orgApi.placeHold).toHaveBeenCalledWith(
        expect.objectContaining({ file_uuids: [F1, F2], matter_ref: 'CASE-9' })
      )
    );
    expect(adminApi.placeHold).not.toHaveBeenCalled();
    expect(screen.getByText(/your organization's administrators/)).toBeInTheDocument();
  });

  it('refuses more than 500 files with its own message and never calls the server', async () => {
    const many = Array.from(
      { length: 501 },
      (_, i) => `00000000-0000-0000-0000-${String(i).padStart(12, '0')}`
    );
    open({ placer: 'platform', fileUuids: many });
    await fireEvent.input(reasonBox(), { target: { value: REASON } });
    expect(screen.getByRole('alert')).toHaveTextContent('at most 500 files');
    expect(submit()).toBeDisabled();
    await fireEvent.click(submit());
    expect(adminApi.placeHold).not.toHaveBeenCalled();
  });

  it('shows the server refusal and stays open when placing fails', async () => {
    adminApi.placeHold.mockRejectedValue({
      response: { status: 404, data: { detail: 'File not found' } },
    });
    open({ placer: 'platform' });
    await fireEvent.input(reasonBox(), { target: { value: REASON } });
    await fireEvent.click(submit());
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('File not found'));
    expect(screen.getByTestId('place-hold-dialog')).toBeInTheDocument();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('names a single file instead of counting it', () => {
    open({ placer: 'platform', fileUuids: [F1], fileLabel: 'Board meeting' });
    expect(screen.getByTestId('place-hold-target')).toHaveTextContent('Board meeting');
  });

  it('includes the explainer', () => {
    open({ placer: 'platform' });
    expect(screen.getByRole('button', { name: 'What does a legal hold do?' })).toBeInTheDocument();
  });
});
