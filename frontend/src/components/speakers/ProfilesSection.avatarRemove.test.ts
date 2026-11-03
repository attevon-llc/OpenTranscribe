/**
 * The remove-avatar control on a speaker profile: offered only when the profile has an avatar of
 * its own, confirmed before the DELETE, and the shown avatar is cleared from the response.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((key: string) => key), locale: readable('en') };
});

const api = vi.hoisted(() => ({
  updateProfile: vi.fn(),
  deleteProfile: vi.fn(),
  uploadProfileAvatar: vi.fn(),
  deleteProfileAvatar: vi.fn(),
  confirmProfileGender: vi.fn(),
}));
vi.mock('$lib/api/speakerClusters', () => api);
const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import ProfilesSection from './ProfilesSection.svelte';
import type { SpeakerProfile } from '$lib/types/speakerCluster';

function profile(extra: Partial<SpeakerProfile> = {}): SpeakerProfile {
  return {
    uuid: 'p1',
    name: 'Ada',
    avatar_url: '/avatars/p1.png',
    instance_count: 1,
    ...extra,
  } as SpeakerProfile;
}

function mount(profiles: SpeakerProfile[]) {
  return render(ProfilesSection, { props: { profiles, active: true, reload: vi.fn() } });
}

beforeEach(() => vi.clearAllMocks());

describe('remove avatar', () => {
  it('offers the control only for an owned profile that has an avatar', () => {
    mount([
      profile(),
      profile({ uuid: 'p2', name: 'Bob', avatar_url: null }),
      profile({ uuid: 'p3', name: 'Cy', is_shared: true }),
    ]);
    expect(screen.getAllByRole('button', { name: 'speakers.avatar.remove' })).toHaveLength(1);
  });

  it('asks first, then deletes the avatar and swaps in the initials', async () => {
    api.deleteProfileAvatar.mockResolvedValue(undefined);
    mount([profile()]);
    await fireEvent.click(screen.getByRole('button', { name: 'speakers.avatar.remove' }));
    expect(api.deleteProfileAvatar).not.toHaveBeenCalled();
    expect(document.querySelector('img.profile-avatar')).not.toBeNull();

    await fireEvent.click(
      await screen.findByRole('button', { name: 'speakers.avatar.removeConfirm' })
    );

    await waitFor(() => expect(api.deleteProfileAvatar).toHaveBeenCalledWith('p1'));
    await waitFor(() => expect(document.querySelector('img.profile-avatar')).toBeNull());
    expect(toast.success).toHaveBeenCalledWith('speakers.avatar.removed');
  });

  it('keeps the avatar and toasts the server reason when the delete fails', async () => {
    api.deleteProfileAvatar.mockRejectedValue({ response: { data: { detail: 'Not allowed' } } });
    mount([profile()]);
    await fireEvent.click(screen.getByRole('button', { name: 'speakers.avatar.remove' }));
    await fireEvent.click(
      await screen.findByRole('button', { name: 'speakers.avatar.removeConfirm' })
    );

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Not allowed'));
    expect(document.querySelector('img.profile-avatar')).not.toBeNull();
  });
});
