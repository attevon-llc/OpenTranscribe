/**
 * Switching the transcription worker to another local model (issue #1246): set the model, ask the
 * worker to restart (it finishes running jobs first), then poll until the worker itself reports
 * the new model. Nothing here decides the switch worked except that report.
 */

export interface LoadedModel {
  worker: string;
  model: string;
  ready_at: string;
}

export interface RestartReply {
  model: string;
  workers: string[];
  active_tasks?: number;
  restart_requested_at: string;
}

export interface ActivationApi {
  setLocalModel(modelId: string): Promise<unknown>;
  restartGpuWorker(): Promise<RestartReply>;
  getLoadedModels(): Promise<LoadedModel[]>;
}

/**
 * Confirmed when EVERY worker named in the restart reply reports the new model, ready after the
 * restart was requested. Entries for other workers are never waited on.
 */
export function switchConfirmed(restart: RestartReply, loaded: LoadedModel[]): boolean {
  const requestedAt = Date.parse(restart.restart_requested_at);
  if (Number.isNaN(requestedAt) || restart.workers.length === 0) return false;
  return restart.workers.every((worker) =>
    loaded.some(
      (entry) =>
        entry.worker === worker &&
        entry.model === restart.model &&
        Date.parse(entry.ready_at) > requestedAt
    )
  );
}

export type ActivationOutcome = 'confirmed' | 'pending' | 'aborted';

export interface ActivationOptions {
  api: ActivationApi;
  /** Called once the restart is signalled, with the number of jobs still running. */
  onRestarted: (activeTasks: number) => void;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
  intervalMs?: number;
  timeoutMs?: number;
  signal?: AbortSignal;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Rejects when setting or restarting is refused (nothing is restarted if setting fails).
 * Resolves 'pending' when the worker has not reported in time: it is most likely still waiting
 * for running jobs, which is not a failure.
 */
export async function activateLocalModel(
  modelId: string,
  options: ActivationOptions
): Promise<ActivationOutcome> {
  const {
    api,
    onRestarted,
    sleep = defaultSleep,
    now = Date.now,
    intervalMs = 3000,
    timeoutMs = 5 * 60_000,
    signal,
  } = options;

  await api.setLocalModel(modelId);
  const restart = await api.restartGpuWorker();
  onRestarted(restart.active_tasks ?? 0);

  const deadline = now() + timeoutMs;
  while (now() < deadline) {
    await sleep(intervalMs);
    if (signal?.aborted) return 'aborted';
    let loaded: LoadedModel[] = [];
    try {
      loaded = await api.getLoadedModels();
    } catch {
      // The API may be briefly unreachable while the stack settles; keep waiting.
    }
    if (signal?.aborted) return 'aborted';
    if (switchConfirmed(restart, loaded)) return 'confirmed';
  }
  return 'pending';
}
