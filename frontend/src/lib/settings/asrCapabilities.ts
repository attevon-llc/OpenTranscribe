/**
 * What a catalog provider/model returns (speakers, timing), read off the ASR provider catalog,
 * and which of the user's saved configs can serve as the speaker detector (issue #1227).
 *
 * The catalog is the server's: a model-level value wins over its provider's, exactly as
 * `ASRProviderFactory.get_model_capabilities` resolves it. Nothing is inferred from names.
 */
import { TIMING_KEYS } from './pipelineKeys';
import type {
  ASRProviderInfo,
  SpeakerCountHint,
  TimestampGranularity,
  UserASRSettingsResponse,
} from '$lib/api/asrSettings';

export interface ModelCapability {
  returnsSpeakers: boolean;
  timing: TimestampGranularity;
  speakerHints: SpeakerCountHint[];
}

/** Null when the catalog does not list the provider. */
export function modelCapability(
  providers: ASRProviderInfo[],
  providerId: string,
  modelId: string
): ModelCapability | null {
  const provider = providers.find((p) => p.id === providerId);
  if (!provider) return null;
  const model = provider.models.find((m) => m.id === modelId);
  return {
    returnsSpeakers: model?.supports_diarization ?? provider.supports_diarization,
    timing: model?.timestamp_granularity ?? provider.timestamp_granularity,
    speakerHints: model?.speaker_count_hints ?? provider.speaker_count_hints,
  };
}

/**
 * What every model of a provider returns when they agree, `'varies'` when they differ (OpenAI's
 * `-diarize` model is the only one of its family that returns speakers), null for a provider
 * the catalog does not list.
 */
export function providerCapability(
  providers: ASRProviderInfo[],
  providerId: string
): ModelCapability | 'varies' | null {
  const provider = providers.find((p) => p.id === providerId);
  if (!provider) return null;
  const all = provider.models
    .map((m) => modelCapability(providers, providerId, m.id))
    .filter((c): c is ModelCapability => c !== null);
  if (all.length === 0) return modelCapability(providers, providerId, '');
  const [first] = all;
  const same = all.every(
    (c) => c.returnsSpeakers === first.returnsSpeakers && c.timing === first.timing
  );
  return same ? first : 'varies';
}

type Translate = (key: string, vars?: Record<string, string>) => string;

/**
 * The badge words as one line for places that cannot draw pills (a native `<option>`), still
 * one translated sentence per locale: "Speakers included, Word timing".
 */
export function capabilityLabel(
  capability: Pick<ModelCapability, 'returnsSpeakers' | 'timing'>,
  translate: Translate
): string {
  return translate('settings.asrProvider.flagsLine', {
    speakers: translate(
      capability.returnsSpeakers
        ? 'settings.asrProvider.speakersIncluded'
        : 'settings.asrProvider.textOnly'
    ),
    timing: translate(TIMING_KEYS[capability.timing]),
  });
}

/**
 * A native option label that carries the flags: `name (flags)`, one string per locale.
 * `'varies'` (a provider whose models differ) says so instead of claiming one set.
 */
export function optionWithFlags(
  name: string,
  capability: Pick<ModelCapability, 'returnsSpeakers' | 'timing'> | 'varies' | null,
  translate: Translate
): string {
  if (!capability) return name;
  if (capability === 'varies') {
    return translate('settings.asrProvider.optionVaries', { name });
  }
  return translate('settings.asrProvider.optionWithFlags', {
    name,
    flags: capabilityLabel(capability, translate),
  });
}

/** Where the speaker labels of a model come from; decides which sentence explains it. */
export interface CapabilityContext {
  /** The model runs on this server (its speakers come from this server's detector). */
  local?: boolean;
  /** This deployment has no local speaker detection (lite). */
  lite?: boolean;
}

export interface CapabilityNote {
  key: string;
  /** A hard stop (the model cannot be combined with speaker detection). */
  blocking: boolean;
}

/**
 * What a model's flags mean for mixing and matching, as copy keys in reading order. Pure
 * reading of the flags (the server's plan stays the authority and refuses what cannot run):
 * no timing blocks speaker detection, text only needs another detector, segment timing
 * loses a speaker change inside one segment when a different detector adds the speakers.
 */
export function capabilityNotes(
  capability: Pick<ModelCapability, 'returnsSpeakers' | 'timing'>,
  context: CapabilityContext = {}
): CapabilityNote[] {
  if (capability.timing === 'none') {
    return [{ key: 'settings.asrProvider.timing.noneInfo', blocking: true }];
  }
  const notes: CapabilityNote[] = [];
  if (capability.returnsSpeakers) {
    notes.push({
      key: context.local
        ? 'settings.asrProvider.implication.speakersLocal'
        : 'settings.asrProvider.implication.speakersIncluded',
      blocking: false,
    });
  } else {
    notes.push({
      key: context.lite
        ? 'settings.asrProvider.implication.textOnlyLite'
        : 'settings.asrProvider.implication.textOnly',
      blocking: false,
    });
    if (capability.timing === 'segment') {
      notes.push({ key: 'settings.pipeline.issue.segment_timing_only', blocking: false });
    }
  }
  return notes;
}

export interface DiarizerChoice {
  uuid: string;
  name: string;
  /** Catalog id of the provider (compared with the transcribing one), e.g. `deepgram`. */
  provider: string;
  providerLabel: string;
  modelName: string;
  /** Owner of a config shared with the user; null for the user's own. */
  owner: string | null;
  returnsSpeakers: boolean;
  /** Null for a model the catalog does not list: nothing is claimed about its timing. */
  timing: TimestampGranularity | null;
}

/** A saved config as the speaker detector picker lists it, qualifying or not. */
export interface SavedProviderRow extends DiarizerChoice {
  /** The config that transcribes now; choosing it is Automatic. */
  transcribing: boolean;
}

/**
 * Every saved config with what it returns, so the picker can say which ones qualify as the
 * speaker detector and why the rest do not. A config the catalog does not list reads as text
 * only, with no claim about timing.
 */
export function savedProviderRows(
  own: UserASRSettingsResponse[],
  shared: UserASRSettingsResponse[],
  providers: ASRProviderInfo[],
  activeUuid: string | null
): SavedProviderRow[] {
  const rows: SavedProviderRow[] = [];
  for (const config of [...own, ...shared]) {
    if (rows.some((row) => row.uuid === config.uuid)) continue;
    const capability = modelCapability(providers, config.provider, config.model_name);
    const provider = providers.find((p) => p.id === config.provider);
    rows.push({
      uuid: config.uuid,
      name: config.name,
      provider: config.provider,
      providerLabel: provider?.display_name ?? config.provider,
      modelName: config.model_name,
      owner: config.is_own === false ? config.owner_name ?? null : null,
      returnsSpeakers: capability?.returnsSpeakers ?? false,
      timing: capability?.timing ?? null,
      transcribing: config.uuid === activeUuid,
    });
  }
  return rows;
}

/**
 * The saved configs that can be chosen as the speaker detector: models that return speakers,
 * not the one that transcribes (choosing that one is Automatic).
 */
export function diarizerChoices(
  own: UserASRSettingsResponse[],
  shared: UserASRSettingsResponse[],
  providers: ASRProviderInfo[],
  activeUuid: string | null
): DiarizerChoice[] {
  return savedProviderRows(own, shared, providers, activeUuid).filter(
    (row) => row.returnsSpeakers && !row.transcribing
  );
}
