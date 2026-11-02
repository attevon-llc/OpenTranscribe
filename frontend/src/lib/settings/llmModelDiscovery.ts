import { LLMSettingsApi, type ProviderDefaults } from '$lib/api/llmSettings';
import { toastStore } from '$stores/toast';
import { getErrorMessage } from '$lib/utils/apiError';

export type OllamaModel = Awaited<
  ReturnType<typeof LLMSettingsApi.getOllamaModels>
>['models'][number];
export type OpenAIModel = Awaited<
  ReturnType<typeof LLMSettingsApi.getOpenAICompatibleModels>
>['models'][number];
export type AnthropicModel = Awaited<
  ReturnType<typeof LLMSettingsApi.getAnthropicModels>
>['models'][number];

/** Translate an i18n key; the modal passes its `$t`. */
type Translate = (key: string) => string;

interface DiscoveryContext {
  t: Translate;
  /** Toggle the "loading models" flag around the request. */
  setLoading: (loading: boolean) => void;
}

/**
 * Run a model-list request: toast a refusal (`success: false`) or a transport error, and give
 * back the models only when the endpoint answered with some.
 */
async function fetchModels<M>(
  ctx: DiscoveryContext,
  request: () => Promise<{ success: boolean; models: M[]; message: string }>,
  failureKey: string
): Promise<M[] | null> {
  ctx.setLoading(true);
  try {
    const result = await request();
    if (result.success && result.models) return result.models;
    toastStore.error(result.message);
  } catch (err: unknown) {
    toastStore.error(getErrorMessage(err, ctx.t(failureKey)));
  } finally {
    ctx.setLoading(false);
  }
  return null;
}

/** The models of an Ollama server, or null (after toasting why). */
export async function discoverOllamaModels(
  ctx: DiscoveryContext,
  baseUrl: string
): Promise<OllamaModel[] | null> {
  if (!baseUrl) {
    toastStore.error(ctx.t('llm.enterBaseUrlFirst'));
    return null;
  }
  return fetchModels(ctx, () => LLMSettingsApi.getOllamaModels(baseUrl), 'llm.ollamaLoadFailed');
}

/** The models of an OpenAI-compatible endpoint, or null (after toasting why). */
export async function discoverOpenAIModels(
  ctx: DiscoveryContext,
  form: { baseUrl: string; apiKey: string; provider: ProviderDefaults | undefined },
  editing: { uuid: string; hasApiKey: boolean } | null
): Promise<OpenAIModel[] | null> {
  if (!form.baseUrl) {
    toastStore.error(ctx.t('llm.enterBaseUrlFirst'));
    return null;
  }
  // Without a stored key (edit mode), a provider that needs one needs it typed
  if (form.provider?.requires_api_key && !form.apiKey?.trim() && !editing?.hasApiKey) {
    toastStore.error(ctx.t('llm.enterApiKeyFirst'));
    return null;
  }
  return fetchModels(
    ctx,
    () =>
      LLMSettingsApi.getOpenAICompatibleModels(
        form.baseUrl,
        form.apiKey?.trim() || undefined,
        editing?.uuid // edit mode: the backend uses the stored key
      ),
    'llm.modelsLoadFailed'
  );
}

/** The models of an Anthropic account, or null (after toasting why). */
export async function discoverAnthropicModels(
  ctx: DiscoveryContext,
  apiKey: string,
  editing: { uuid: string; hasApiKey: boolean } | null
): Promise<AnthropicModel[] | null> {
  // In edit mode the stored key is used when no new one is entered
  if (!(apiKey?.trim() || editing?.hasApiKey)) {
    toastStore.error(ctx.t('llm.enterApiKeyFirst'));
    return null;
  }
  return fetchModels(
    ctx,
    () =>
      LLMSettingsApi.getAnthropicModels(
        apiKey?.trim() || undefined, // only send a key the user typed
        editing?.uuid // the backend uses the stored key when none is sent
      ),
    'llm.modelsLoadFailed'
  );
}
