/**
 * Display labels for values the backend sends as a stable code plus an English label.
 *
 * The backend owns the vocabulary (which codes exist); the reader's language is a display choice,
 * so the label is resolved here: a translated key, or `Intl.DisplayNames` for language names
 * (https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Intl/DisplayNames).
 * The backend's English label is the fallback for any code this build has no string for, so a
 * code added server-side still renders as words, never as a dotted key.
 */
import type { Translate } from '$lib/i18n';

const has = (map: object, value: string) => Object.prototype.hasOwnProperty.call(map, value);

function mapped(
  t: Translate,
  map: Readonly<Record<string, string>>,
  code: string | null | undefined,
  fallback: string
): string {
  return typeof code === 'string' && has(map, code) ? t(map[code] as string) : fallback;
}

/** Whisper's language codes that are not the BCP 47 code `Intl` knows the language by. */
const WHISPER_TO_BCP47: Readonly<Record<string, string>> = {
  jw: 'jv', // Javanese: Whisper keeps the deprecated ISO 639-1 code.
};

/** One language name in the UI language, else the backend's own label. */
export function languageLabel(
  t: Translate,
  locale: string,
  code: string,
  backendLabel: string
): string {
  if (code === 'auto') return t('settings.transcription.defaultSourceAuto');
  const bcp47 = WHISPER_TO_BCP47[code] ?? code;
  try {
    const name = new Intl.DisplayNames([locale], { type: 'language', fallback: 'none' }).of(bcp47);
    if (name) return name;
  } catch {
    // A runtime without Intl.DisplayNames, or a code it rejects: keep the server's name.
  }
  return backendLabel;
}

/** A `code -> English name` map from the backend, with every name in the UI language. */
export function localizeLanguages(
  t: Translate,
  locale: string,
  languages: Record<string, string> | null | undefined
): Record<string, string> | null {
  if (!languages) return null;
  return Object.fromEntries(
    Object.entries(languages).map(([code, name]) => [code, languageLabel(t, locale, code, name)])
  );
}

const VIDEO_QUALITY_KEYS: Readonly<Record<string, string>> = {
  best: 'settings.download.bestAvailable',
  '360p': 'settings.download.quality360p',
};

/**
 * A video quality option. Only `best` and the "Low" suffix of 360p are words; the other
 * labels ("1080p (Full HD)", "4K (2160p)") are technical names used as-is in every language.
 */
export function videoQualityLabel(t: Translate, code: string, backendLabel: string): string {
  return mapped(t, VIDEO_QUALITY_KEYS, code, backendLabel);
}

/** An audio bitrate option: `best`, or a kbps code rendered with the locale's unit. */
export function audioQualityLabel(t: Translate, code: string, backendLabel: string): string {
  if (code === 'best') return t('settings.download.bestAvailable');
  return /^\d+$/.test(code) ? t('settings.download.audioBitrate', { kbps: code }) : backendLabel;
}

const DIAR_ENGINE_KEYS: Readonly<Record<string, string>> = {
  native: 'settings.statistics.diarEngine.native',
  pyannote: 'settings.statistics.diarEngine.pyannote',
  unavailable: 'settings.statistics.diarEngine.unavailable',
};

/** A diarization engine (`configured_backend` / `effective_backend`) as a sentence. */
export function diarEngineLabel(
  t: Translate,
  code: string | null | undefined,
  backendDescription: string | null | undefined
): string {
  return mapped(t, DIAR_ENGINE_KEYS, code, backendDescription || code || '');
}

const INTEGRITY_INDEX_KEYS: Readonly<Record<string, string>> = {
  speakers_v3: 'settings.dataIntegrity.indexSpeakersV3',
  speakers_v4: 'settings.dataIntegrity.indexSpeakersV4',
  transcripts: 'settings.dataIntegrity.indexTranscripts',
  summaries: 'settings.dataIntegrity.indexSummaries',
};

/** The Data integrity card title for one index group (`kind` from the backend). */
export function integrityIndexLabel(
  t: Translate,
  kind: string | null | undefined,
  backendLabel: string
): string {
  return mapped(t, INTEGRITY_INDEX_KEYS, kind, backendLabel);
}

const EMBEDDING_MODEL_SLUGS: Readonly<Record<string, string>> = {
  'huggingface/sentence-transformers/all-MiniLM-L6-v2': 'miniLmL6',
  'huggingface/sentence-transformers/all-MiniLM-L12-v2': 'miniLmL12',
  'huggingface/sentence-transformers/multi-qa-MiniLM-L6-cos-v1': 'multiQaMiniLm',
  'huggingface/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2': 'multilingualMiniLm',
  'huggingface/sentence-transformers/all-mpnet-base-v2': 'mpnet',
  'huggingface/sentence-transformers/all-distilroberta-v1': 'distilroberta',
  'huggingface/sentence-transformers/distiluse-base-multilingual-cased-v1': 'distiluse',
};

// Literal keys, so the unused-key check and keyMaps-style tests can see every one of them.
const EMBEDDING_MODEL_KEYS: Readonly<Record<string, { name: string; description: string }>> = {
  miniLmL6: {
    name: 'settings.search.embeddingModel.miniLmL6.name',
    description: 'settings.search.embeddingModel.miniLmL6.description',
  },
  miniLmL12: {
    name: 'settings.search.embeddingModel.miniLmL12.name',
    description: 'settings.search.embeddingModel.miniLmL12.description',
  },
  multiQaMiniLm: {
    name: 'settings.search.embeddingModel.multiQaMiniLm.name',
    description: 'settings.search.embeddingModel.multiQaMiniLm.description',
  },
  multilingualMiniLm: {
    name: 'settings.search.embeddingModel.multilingualMiniLm.name',
    description: 'settings.search.embeddingModel.multilingualMiniLm.description',
  },
  mpnet: {
    name: 'settings.search.embeddingModel.mpnet.name',
    description: 'settings.search.embeddingModel.mpnet.description',
  },
  distilroberta: {
    name: 'settings.search.embeddingModel.distilroberta.name',
    description: 'settings.search.embeddingModel.distilroberta.description',
  },
  distiluse: {
    name: 'settings.search.embeddingModel.distiluse.name',
    description: 'settings.search.embeddingModel.distiluse.description',
  },
};

/** A search embedding model's name or description in the UI language, else the backend's. */
export function embeddingModelText(
  t: Translate,
  modelId: string,
  field: 'name' | 'description',
  backendText: string
): string {
  const slug = EMBEDDING_MODEL_SLUGS[modelId];
  const keys = slug ? EMBEDDING_MODEL_KEYS[slug] : undefined;
  return keys ? t(keys[field]) : backendText;
}

/** The fields of a local Whisper model entry the description is built from. */
export interface LocalModelHints {
  description?: string | null;
  vram_hint_gb?: number | null;
  is_default?: boolean | null;
}

/** "GPU memory: about 6 GB", from the catalog's numbers rather than its English sentence. */
export function localWhisperDescription(
  t: Translate,
  model: LocalModelHints | null | undefined
): string {
  if (!model) return '';
  if (typeof model.vram_hint_gb === 'number') {
    const key = model.is_default ? 'settings.localModels.vramDefault' : 'settings.localModels.vram';
    return t(key, { gb: model.vram_hint_gb });
  }
  return model.description ?? '';
}

const MODEL_PURPOSE_KEYS: Readonly<Record<string, string>> = {
  whisper: 'settings.statistics.modelPurpose.whisper',
  diarization: 'settings.statistics.modelPurpose.diarization',
  search_embedding: 'settings.statistics.modelPurpose.searchEmbedding',
  llm: 'settings.statistics.modelPurpose.llm',
};

/** What a model in the statistics panel is for, keyed by its entry in `stats.models`. */
export function modelPurposeLabel(t: Translate, modelKey: string, backendPurpose: string): string {
  return mapped(t, MODEL_PURPOSE_KEYS, modelKey, backendPurpose);
}
