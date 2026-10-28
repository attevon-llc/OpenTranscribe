import axiosInstance from '$lib/axios';

/**
 * Privacy controls (issue #1280): voiceprint matching and inferred gender.
 *
 * Mirrors `backend/app/schemas/privacy.py`. One restrict-only policy: platform, instance,
 * organization and user layers are ANDed, any layer that is off turns the feature off, and
 * `locked_by` names the highest layer that did. Turning a switch off HIDES data; a purge is the
 * separate, irreversible deletion and the API refuses it (409 `policy_still_enabled`) until the
 * switch is off at the purging layer.
 *
 * The organization routes (`/org-admin/policy`) live on {@link OrgAdminApi}.
 */

/** Every layer a decision can be locked by. `consent` only appears for voiceprint creation. */
export type PrivacyLayer = 'platform' | 'instance' | 'organization' | 'user' | 'consent';
/** The layers ABOVE a user's own switch, i.e. the ones that can lock it. */
export type UserLockLayer = 'platform' | 'instance' | 'organization';
/** What a purge deletes. Named profiles, avatars and people's own assignments are always kept. */
export type PurgeTarget = 'voiceprints' | 'inferred_gender';

/** The literal the API requires in `PurgeRequest.confirm`; the dialog makes the person type it. */
export const PURGE_CONFIRM_WORD = 'PURGE';

export interface PrivacyDecision {
  enabled: boolean;
  /** Null exactly when `enabled` is true. */
  locked_by: PrivacyLayer | null;
}

/** `GET /privacy/effective`: the policy as it applies to one file or to the caller. */
export interface EffectivePrivacy {
  organization_uuid: string | null;
  voiceprint_matching: PrivacyDecision;
  gender_inference: PrivacyDecision;
}

/** The instance layer (`GET|PUT /admin/privacy-policy`). Both default on. */
export interface InstancePrivacyPolicy {
  voiceprint_matching_enabled: boolean;
  gender_inference_enabled: boolean;
}

/** `GET|PUT /privacy/me`: the caller's own voiceprint switch. */
export interface UserPrivacyPolicy {
  voiceprint_matching_enabled: boolean;
  /** The layer above the caller that has it off for the active scope; the switch is then locked. */
  locked_by: UserLockLayer | null;
}

export interface PurgeAccepted {
  task_id: string;
}

export type PurgeState = 'pending' | 'in_progress' | 'completed' | 'failed' | 'skipped';

/** Counts grouped per store: `postgres` (database rows) and `opensearch` (documents). */
export type PurgeCounts = Record<string, Record<string, number>>;

/** Where an administrator reads the purge back: one audit event per target, same task id. */
export interface PurgeAuditRef {
  event_type: string;
  task_id: string;
}

/**
 * `GET /privacy/purge/{task_id}`: counts only, never a name.
 *
 * `residual` is what the latest pass measured in the purged scope afterwards: every value must be
 * 0, and a purge that left something is `failed`, never `completed`. `residual_unverified` names
 * the measures that could not be taken; those are unknown, not zero.
 */
export interface PurgeStatus {
  task_id: string;
  status: PurgeState;
  counts: PurgeCounts | null;
  residual: Record<string, number> | null;
  residual_ok: boolean | null;
  residual_unverified: string[];
  audit: PurgeAuditRef[];
}

export type PurgeScope = 'personal' | 'all';

export function isTerminalPurgeState(state: PurgeState): boolean {
  return state === 'completed' || state === 'failed' || state === 'skipped';
}

export class PrivacyApi {
  /** The policy for a file the caller can see, or for the caller in the active organization. */
  static async getEffective(fileUuid?: string): Promise<EffectivePrivacy> {
    const response = await axiosInstance.get('/privacy/effective', {
      params: fileUuid ? { file_uuid: fileUuid } : undefined,
    });
    return response.data;
  }

  static async getMine(): Promise<UserPrivacyPolicy> {
    const response = await axiosInstance.get('/privacy/me');
    return response.data;
  }

  /** 409 `policy_locked` while a layer above the user has voiceprint matching off. */
  static async setMine(voiceprintMatchingEnabled: boolean): Promise<UserPrivacyPolicy> {
    const response = await axiosInstance.put('/privacy/me', {
      voiceprint_matching_enabled: voiceprintMatchingEnabled,
    });
    return response.data;
  }

  /** Irreversibly delete the caller's own data in the active scope. 202. */
  static async purgeMine(targets: PurgeTarget[]): Promise<PurgeAccepted> {
    const response = await axiosInstance.post('/privacy/me/purge', {
      targets,
      confirm: PURGE_CONFIRM_WORD,
    });
    return response.data;
  }

  /** Readable only by the person who started the purge; anyone else's id is a 404. */
  static async getPurgeStatus(taskId: string): Promise<PurgeStatus> {
    const response = await axiosInstance.get(`/privacy/purge/${encodeURIComponent(taskId)}`);
    return response.data;
  }

  /** The instance layer (super_admin). */
  static async getInstance(): Promise<InstancePrivacyPolicy> {
    const response = await axiosInstance.get('/admin/privacy-policy');
    return response.data;
  }

  /** Partial update of the instance layer (super_admin). */
  static async updateInstance(
    changes: Partial<InstancePrivacyPolicy>
  ): Promise<InstancePrivacyPolicy> {
    const response = await axiosInstance.put('/admin/privacy-policy', changes);
    return response.data;
  }

  /**
   * Instance-wide purge (super_admin). `all` also deletes every tenant's data and is refused with
   * 403 `purge_scope_not_allowed` in a multi-tenant deployment.
   */
  static async purgeInstance(scope: PurgeScope, targets: PurgeTarget[]): Promise<PurgeAccepted> {
    const response = await axiosInstance.post('/admin/privacy-policy/purge', {
      scope,
      targets,
      confirm: PURGE_CONFIRM_WORD,
    });
    return response.data;
  }
}
