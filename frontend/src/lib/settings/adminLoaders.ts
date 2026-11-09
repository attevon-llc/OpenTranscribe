/**
 * Loaders behind the Settings modal's admin sections and sidebar badges.
 *
 * Each resolves to plain data; the modal owns loading flags and toasts. Failure handling that the
 * modal depends on is documented per function.
 */
import axiosInstance from '$lib/axios';
import { SupportAccessApi } from '$lib/api/supportAccess';
import { UserApprovalsApi } from '$lib/api/userApprovals';

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** A model entry of the stats payload. Only `name` is guaranteed; the rest is per-model detail. */
export interface ModelStats {
  name: string;
  description?: string;
  purpose?: string;
}

/** The diarization entry also reports which backend is configured vs. actually running. */
export interface DiarizationModelStats extends ModelStats {
  configured_backend?: string;
  configured_description?: string;
  effective_backend?: string;
  using_fallback?: boolean;
}

/** A recently finished or running task row in the stats payload. */
export interface RecentTaskStats {
  id: string;
  type: string;
  status: string;
  created_at: string;
  elapsed: number;
}

/** The /system/stats payload as SystemStatisticsPanel consumes it. */
export interface SystemStats {
  users: { total: number; new: number };
  files: { total: number; new: number; total_duration: number; segments: number };
  transcripts?: { total_segments_is_estimate?: boolean };
  tasks: {
    total: number;
    pending: number;
    running: number;
    completed: number;
    failed: number;
    success_rate: number;
    avg_processing_time: number;
    recent: RecentTaskStats[];
  };
  speakers: { total: number; avg_per_file: number; total_is_estimate?: boolean };
  models: {
    whisper: ModelStats;
    diarization: DiarizationModelStats;
    search_embedding?: ModelStats;
    llm?: ModelStats;
  };
  system: {
    cpu: {
      total_percent: string;
      per_cpu: number[];
      logical_cores: number;
      physical_cores: number;
    };
    memory: { total: string; available: string; used: string; percent: string };
    disk: { total: string; used: string; free: string; percent: string };
    gpus: GpuStats[];
    uptime: string;
    platform: string;
    python_version: string;
    whisper_model?: string;
    device_mode?: string;
    force_cpu_mode?: boolean;
    chat_reranker_threads?: { threads: number | null; source: string; setting?: string };
  };
  throughput: {
    total_completed: number;
    last_1h: number;
    last_3h: number;
    rate_1h: number;
    rate_3h: number;
  };
  eta: {
    remaining: number;
    files_per_hour: number;
    hours_remaining: number | null;
    est_completion: string | null;
  };
  file_timing: {
    files: number;
    avg_secs: number;
    min_secs: number;
    max_secs: number;
    avg_mins: number;
  };
  queues: Record<string, number>;
}

export interface SearchIndexStatus {
  indexed_files: number;
  total_files: number;
  pending_files: number;
  in_progress: boolean;
  current_model: string;
}
export type SearchHealthStatus = Record<string, { status: string; doc_count: number }>;

/** One GPU entry of the stats payload; `loading` is set by the backend while it warms up. */
interface GpuStats {
  available: boolean;
  name: string;
  memory_total: string;
  memory_used: string;
  memory_free: string;
  memory_percent: string;
  utilization_percent: string;
  temperature_celsius: number | null;
  loading?: boolean;
}

/** A row of `GET /admin/users`; the users table reads the fields it needs. */
export type AdminUserRow = Record<string, unknown>;

/** Placeholder stats shown before the first load, so the panel renders with zeros, not undefined. */
export function initialSystemStats(t: Translate): SystemStats {
  return {
    users: { total: 0, new: 0 },
    files: { total: 0, new: 0, total_duration: 0, segments: 0 },
    tasks: {
      total: 0,
      pending: 0,
      running: 0,
      completed: 0,
      failed: 0,
      success_rate: 0,
      avg_processing_time: 0,
      recent: [],
    },
    speakers: { total: 0, avg_per_file: 0 },
    models: {
      whisper: {
        name: t('common.notAvailable'),
        description: t('common.notAvailable'),
        purpose: t('common.notAvailable'),
      },
      diarization: {
        name: t('common.notAvailable'),
        description: t('common.notAvailable'),
        purpose: t('common.notAvailable'),
      },
    },
    system: {
      cpu: { total_percent: '0%', per_cpu: [], logical_cores: 0, physical_cores: 0 },
      memory: { total: '0 B', available: '0 B', used: '0 B', percent: '0%' },
      disk: { total: '0 B', used: '0 B', free: '0 B', percent: '0%' },
      // These stay the untranslated literal "N/A" on purpose: it is the backend's
      // wire sentinel for an unreadable GPU metric (admin.py / tasks/utility.py) and
      // SystemStatisticsPanel compares `utilization_percent !== 'N/A'` against it.
      // The placeholder entry is `available: false`, so none of it ever renders.
      gpus: [
        {
          available: false,
          name: 'N/A',
          memory_total: 'N/A',
          memory_used: 'N/A',
          memory_free: 'N/A',
          memory_percent: 'N/A',
          utilization_percent: 'N/A',
          temperature_celsius: null,
        },
      ],
      uptime: t('common.unknown'),
      platform: t('common.unknown'),
      python_version: t('common.unknown'),
    },
    throughput: { total_completed: 0, last_1h: 0, last_3h: 0, rate_1h: 0, rate_3h: 0 },
    eta: { remaining: 0, files_per_hour: 0, hours_remaining: null, est_completion: null },
    file_timing: { files: 0, avg_secs: 0, min_secs: 0, max_secs: 0, avg_mins: 0 },
    queues: { gpu: 0, download: 0, nlp: 0, embedding: 0, cpu: 0, utility: 0, total: 0 },
  };
}

/**
 * Accounts awaiting an approval decision. A failed fetch counts 0: an admin whose deployment
 * 404s or 403s this endpoint should see no badge, not a permanent phantom count.
 */
export async function countPendingApprovals(): Promise<number> {
  try {
    return (await UserApprovalsApi.list()).length;
  } catch {
    return 0;
  }
}

/**
 * Support-access requests awaiting this user's decision (issue #1122), across their personal
 * workspace and, for an organization admin, their organization. Zero outside multi-tenant mode.
 */
export async function countSupportRequests(
  tenancyMulti: boolean,
  orgTab: boolean
): Promise<number> {
  if (!tenancyMulti) return 0;
  const pending = { status: 'pending' as const, limit: 1, offset: 0 };
  try {
    const pages = await Promise.all([
      SupportAccessApi.listRequests('workspace', pending),
      ...(orgTab ? [SupportAccessApi.listRequests('org', pending)] : []),
    ]);
    return pages.reduce((sum, page) => sum + page.total, 0);
  } catch {
    return 0;
  }
}

export async function fetchAdminUsers(): Promise<AdminUserRow[]> {
  const response = await axiosInstance.get('/admin/users');
  return response.data;
}

export async function recoverUserFilesRequest(userId: string): Promise<void> {
  await axiosInstance.post(`/tasks/system/recover-user-files/${userId}`);
}

/** The stats payload plus the two optional search-index probes, which may each be absent. */
export async function fetchSystemStats(): Promise<{
  stats: SystemStats;
  indexStatus: SearchIndexStatus | null;
  indexHealth: SearchHealthStatus | null;
}> {
  const [statsRes, indexRes, healthRes] = await Promise.all([
    axiosInstance.get('/system/stats'),
    axiosInstance.get('/search/reindex/status').catch(() => null),
    axiosInstance.get('/search/index-health').catch(() => null),
  ]);
  return {
    stats: statsRes.data,
    indexStatus: indexRes?.data ?? null,
    indexHealth: healthRes?.data ?? null,
  };
}
