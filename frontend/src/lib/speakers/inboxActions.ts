import { batchVerifySpeakers } from '$lib/api/speakerClusters';

export type InboxActionType = 'accept' | 'assign' | 'skip';
export type InboxActionOutcome = 'accepted' | 'assigned' | 'skipped' | 'ignored';

/**
 * Perform one inbox action against the backend.
 *
 * A per-speaker Inbox action is the user's explicit decision about THIS speaker, so accept and
 * assign override an earlier unassignment of it (#1233). A refusal comes back as `failed_count`
 * and is thrown here, so it can never read as success. `ignored` means the action needed
 * something it did not have (e.g. `assign` without a profile) and nothing was sent.
 */
export async function performInboxAction(
  type: string,
  speakerUuid: string,
  profileUuid: string | undefined,
  suggestedName: string | null | undefined
): Promise<InboxActionOutcome> {
  const explicit = true;
  if (type === 'accept') {
    const result = await batchVerifySpeakers(
      [speakerUuid],
      'accept',
      undefined,
      suggestedName || undefined,
      explicit
    );
    if (result.failed_count > 0) throw new Error('verify refused');
    return 'accepted';
  }
  if (type === 'assign' && profileUuid) {
    const result = await batchVerifySpeakers(
      [speakerUuid],
      'assign',
      profileUuid,
      undefined,
      explicit
    );
    if (result.failed_count > 0) throw new Error('verify refused');
    return 'assigned';
  }
  if (type === 'skip') {
    const result = await batchVerifySpeakers([speakerUuid], 'skip');
    if (result.failed_count > 0) throw new Error('verify refused');
    return 'skipped';
  }
  return 'ignored';
}
