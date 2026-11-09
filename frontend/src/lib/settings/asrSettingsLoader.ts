/**
 * Everything the ASR settings panel shows on load, fetched in one round and shaped for it.
 * What a user cannot use (a capability the deployment turned off) is not requested at all, and
 * the reads that may legitimately fail (a lite deployment has no local model, a user may have
 * no saved settings) fall back to an empty answer instead of failing the whole panel.
 */
import {
  ASRSettingsApi,
  type ASRProviderInfo,
  type UserASRSettingsResponse,
} from '$lib/api/asrSettings';
import { getTranscriptionSettings, type DiarizationSource } from '$lib/api/transcriptionSettings';

type ActiveLocalModel = Awaited<ReturnType<typeof ASRSettingsApi.getActiveLocalModel>>;

export interface AsrSettingsBundle {
  providers: ASRProviderInfo[];
  configurations: UserASRSettingsResponse[];
  sharedConfigurations: UserASRSettingsResponse[];
  activeConfigurationId: string | null;
  /** True when no saved provider is active, so the local model transcribes. */
  usingLocalDefault: boolean;
  isLite: boolean;
  diarizationSource: DiarizationSource | null;
  diarizerConfigUuid: string | null;
  localModel: ActiveLocalModel | null;
}

const NO_SETTINGS = {
  configurations: [],
  shared_configurations: [],
  active_configuration_id: undefined,
  total: 0,
};

export async function loadAsrSettings(caps: {
  userProviders: boolean;
  modelSelection: boolean;
}): Promise<AsrSettingsBundle> {
  const [providersResp, settingsResp, localModelResp, statusResp, speakerSettings] =
    await Promise.all([
      caps.userProviders ? ASRSettingsApi.getProviders() : Promise.resolve({ providers: [] }),
      caps.userProviders
        ? ASRSettingsApi.getSettings().catch(() => NO_SETTINGS)
        : Promise.resolve(NO_SETTINGS),
      caps.modelSelection
        ? ASRSettingsApi.getActiveLocalModel().catch(() => null)
        : Promise.resolve(null),
      caps.userProviders ? ASRSettingsApi.getStatus().catch(() => null) : Promise.resolve(null),
      getTranscriptionSettings().catch(() => null),
    ]);
  const activeConfigurationId = settingsResp.active_configuration_id || null;
  return {
    providers: providersResp.providers,
    configurations: settingsResp.configurations,
    sharedConfigurations: settingsResp.shared_configurations || [],
    activeConfigurationId,
    usingLocalDefault: settingsResp.configurations.length === 0 || !activeConfigurationId,
    isLite: statusResp?.deployment_mode === 'lite',
    diarizationSource: speakerSettings?.diarization_source ?? null,
    diarizerConfigUuid: speakerSettings?.diarization_asr_config_uuid ?? null,
    localModel: localModelResp,
  };
}
