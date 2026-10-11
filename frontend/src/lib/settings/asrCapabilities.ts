/**
 * What a catalog provider/model returns (speakers, timing), read off the ASR provider catalog,
 * and which of the user's saved configs can serve as the speaker detector (issue #1227).
 *
 * The catalog is the server's: a model-level value wins over its provider's, exactly as
 * `ASRProviderFactory.get_model_capabilities` resolves it. Nothing is inferred from names.
 */
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

export interface DiarizerChoice {
  uuid: string;
  name: string;
  providerLabel: string;
  modelName: string;
  /** Owner of a config shared with the user; null for the user's own. */
  owner: string | null;
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
  const choices: DiarizerChoice[] = [];
  for (const config of [...own, ...shared]) {
    if (config.uuid === activeUuid || choices.some((c) => c.uuid === config.uuid)) continue;
    const capability = modelCapability(providers, config.provider, config.model_name);
    if (!capability?.returnsSpeakers) continue;
    const provider = providers.find((p) => p.id === config.provider);
    choices.push({
      uuid: config.uuid,
      name: config.name,
      providerLabel: provider?.display_name ?? config.provider,
      modelName: config.model_name,
      owner: config.is_own === false ? config.owner_name ?? null : null,
    });
  }
  return choices;
}
