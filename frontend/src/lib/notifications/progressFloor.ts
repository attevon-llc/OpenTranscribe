/**
 * Progress on a processing card only rises within one run.
 *
 * The card's percent is a weighted mean over the tasks it currently knows. That set changes
 * under it: the plan names tasks the card did not know about, an LLM turns out to be
 * configured, a failed task freezes at the progress it reached. Each of those can lower the
 * mean, and a bar that moved 95% -> 93% (or 90% -> 81%) reads as the work going backwards.
 * This remembers the highest value shown per card and run, and a new run (a reprocess, a
 * retry: a fresh transcription start) starts over.
 */
import type { ProcessingJobModel } from './processingJob';

const peaks = new Map<string, { run: number | undefined; percent: number }>();

/** The model with its percent held at the highest value this card has shown for this run. */
export function withMonotonicProgress(model: ProcessingJobModel): ProcessingJobModel {
  const seen = peaks.get(model.key);
  const sameRun = seen !== undefined && seen.run === model.run;
  const percent = sameRun ? Math.max(seen.percent, model.percent) : model.percent;
  peaks.set(model.key, { run: model.run, percent });
  return percent === model.percent ? model : { ...model, percent };
}

/** Forget every card's peak: on a session or tenant change (`clearUserState`), and in tests. */
export function resetProgressFloor(): void {
  peaks.clear();
}
