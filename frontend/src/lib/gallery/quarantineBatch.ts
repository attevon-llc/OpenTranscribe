/**
 * The takedown loop: one quarantine call per file, run sequentially and never in parallel,
 * because a takedown is a legally significant action and ordering matters in an audit log
 * (each call is its own audit event with its own reason/IP/user-agent).
 */
import { AdminApi } from '$lib/api/admin';
import { mediaRevocationIncomplete } from '$lib/takedownRevocation';
import { getErrorMessage } from '$lib/utils/apiError';

export interface QuarantineBatchResult {
  succeededUuids: string[];
  failedCount: number;
  /**
   * Taken down, but storage refused to revoke links already handed out (the server says so in
   * `media_revocation`); never counted as a clean success.
   */
  unrevokedCount: number;
}

type QuarantineBasis = Parameters<typeof AdminApi.quarantineFile>[3];

export async function quarantineFiles(
  files: ReadonlyArray<{ uuid: string }>,
  reason: string,
  legalHold: boolean,
  basis: QuarantineBasis
): Promise<QuarantineBatchResult> {
  const result: QuarantineBatchResult = { succeededUuids: [], failedCount: 0, unrevokedCount: 0 };
  for (const file of files) {
    try {
      const response = await AdminApi.quarantineFile(file.uuid, reason, legalHold, basis);
      result.succeededUuids.push(file.uuid);
      if (mediaRevocationIncomplete(response)) result.unrevokedCount += 1;
    } catch (err) {
      result.failedCount += 1;
      // One failed file must not read as a failed batch: log it and carry on.
      console.error(`Quarantine failed for ${file.uuid}:`, getErrorMessage(err));
    }
  }
  return result;
}
