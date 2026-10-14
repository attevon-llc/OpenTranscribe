/**
 * The per-file transcription model choices of the upload wizard and the reprocess dialog, with
 * what each one returns (issue #1243).
 *
 * "Use my setting" is whatever the user's active transcription would run (their saved provider
 * and model, else this server's model), and "Fast" is the lightweight local model. Their speaker
 * and timing flags come from the same ASR catalog as the Settings pickers: nothing is named
 * here by hand, so a catalog change reaches every surface.
 */
import { ASRSettingsApi, type ASRProviderInfo, type ASRStatusResponse } from '$lib/api/asrSettings';
import { modelCapability, type ModelCapability } from './asrCapabilities';

/** The one model the Fast option requests (the server routes it to the CPU worker). */
export const FAST_MODEL = 'base';

export interface PerFileFacts {
  providers: ASRProviderInfo[];
  status: ASRStatusResponse;
}

export interface PerFileOption {
  /** What is sent as `whisper_model`; null keeps the user's transcription setting. */
  value: string | null;
  kind: 'default' | 'fast';
  /** Runs on this server (its speakers come from this server's detector). */
  local: boolean;
  /** Cloud vendor shown in the label; null for this server. */
  vendor: string | null;
  model: string;
  /** Null when the catalog does not list the model: no flags are claimed. */
  capability: ModelCapability | null;
}

/** The option's name as ONE translated string (no fragments glued together). */
export function optionName(
  option: PerFileOption,
  translate: (key: string, vars?: Record<string, string>) => string
): string {
  if (option.kind === 'fast') return translate('uploader.fastProcessingOption');
  return option.vendor
    ? translate('uploader.cloudDefaultOption', { provider: option.vendor, model: option.model })
    : translate('uploader.highQualityOption', { model: option.model });
}

/** Null when either call fails: the picker then shows its options without flags. */
export async function loadPerFileFacts(): Promise<PerFileFacts | null> {
  try {
    const [catalog, status] = await Promise.all([
      ASRSettingsApi.getProviders(),
      ASRSettingsApi.getStatus(),
    ]);
    return { providers: catalog.providers, status };
  } catch {
    return null;
  }
}

/**
 * The options in display order. Lite transcribes in the cloud only, so it has no Fast option
 * (the server refuses a per-file model there).
 */
export function perFileOptions(facts: PerFileFacts | null, adminModel: string): PerFileOption[] {
  const status = facts?.status;
  const providers = facts?.providers ?? [];
  const cloud = status?.is_cloud_provider === true && !!status.active_provider;
  const providerId = cloud ? (status?.active_provider as string) : 'local';
  const model = cloud ? status?.active_model ?? '' : adminModel;
  const info = providers.find((p) => p.id === providerId);
  const options: PerFileOption[] = [
    {
      value: null,
      kind: 'default',
      local: !cloud,
      vendor: cloud ? info?.display_name ?? providerId : null,
      model,
      capability: facts ? modelCapability(providers, providerId, model) : null,
    },
  ];
  if (status?.deployment_mode !== 'lite') {
    options.push({
      value: FAST_MODEL,
      kind: 'fast',
      local: true,
      vendor: null,
      model: FAST_MODEL,
      capability: facts ? modelCapability(providers, 'local', FAST_MODEL) : null,
    });
  }
  return options;
}
