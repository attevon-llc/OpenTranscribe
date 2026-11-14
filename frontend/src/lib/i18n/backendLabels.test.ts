/**
 * Backend display labels resolve to the reader's language by their stable code, and keep the
 * backend's English label only for a code this build has no string for.
 */
import { describe, it, expect } from 'vitest';
import en from '$lib/i18n/locales/en.json';
import {
  audioQualityLabel,
  diarEngineLabel,
  embeddingModelText,
  integrityIndexLabel,
  languageLabel,
  localWhisperDescription,
  localizeLanguages,
  modelPurposeLabel,
  videoQualityLabel,
} from './backendLabels';

const t = (key: string, vars?: Record<string, unknown>) =>
  vars ? `${key} ${JSON.stringify(vars)}` : key;
const english = en as Record<string, string>;

describe('language names', () => {
  it('names a language in the UI language, not the backend English', () => {
    const arabic = new Intl.DisplayNames(['ar'], { type: 'language' }).of('es');
    expect(languageLabel(t, 'ar', 'es', 'Spanish')).toBe(arabic);
    expect(languageLabel(t, 'de', 'es', 'Spanish')).toBe('Spanisch');
  });

  it('translates auto-detect through its key', () => {
    expect(languageLabel(t, 'fr', 'auto', 'Auto-detect')).toBe(
      'settings.transcription.defaultSourceAuto'
    );
  });

  it("maps Whisper's deprecated Javanese code to the one Intl knows", () => {
    expect(languageLabel(t, 'de', 'jw', 'Javanese')).toBe('Javanisch');
  });

  it('keeps the backend label for a code Intl has no name for', () => {
    expect(languageLabel(t, 'de', 'qaa', 'Some Language')).toBe('Some Language');
  });

  it('localizes a whole map and passes null through', () => {
    expect(localizeLanguages(t, 'de', { auto: 'Auto-detect', fr: 'French' })).toEqual({
      auto: 'settings.transcription.defaultSourceAuto',
      fr: 'Französisch',
    });
    expect(localizeLanguages(t, 'de', null)).toBeNull();
  });
});

describe('download qualities', () => {
  it('translates best and the 360p word, leaving technical names as sent', () => {
    expect(videoQualityLabel(t, 'best', 'Best Available')).toBe('settings.download.bestAvailable');
    expect(videoQualityLabel(t, '360p', '360p (Low)')).toBe('settings.download.quality360p');
    expect(videoQualityLabel(t, '1080p', '1080p (Full HD)')).toBe('1080p (Full HD)');
  });

  it('renders a kbps code with the locale unit and keeps an unknown code', () => {
    expect(audioQualityLabel(t, 'best', 'Best Available')).toBe('settings.download.bestAvailable');
    expect(audioQualityLabel(t, '192', '192 kbps')).toBe(
      'settings.download.audioBitrate {"kbps":"192"}'
    );
    expect(audioQualityLabel(t, 'lossless', 'Lossless')).toBe('Lossless');
  });
});

describe('engine, index, model and purpose labels', () => {
  it('maps each diarization engine code, falling back to the server description', () => {
    expect(diarEngineLabel(t, 'native', 'x')).toBe('settings.statistics.diarEngine.native');
    expect(diarEngineLabel(t, 'pyannote', 'x')).toBe('settings.statistics.diarEngine.pyannote');
    expect(diarEngineLabel(t, 'unavailable', 'x')).toBe(
      'settings.statistics.diarEngine.unavailable'
    );
    expect(diarEngineLabel(t, 'future', 'Future engine')).toBe('Future engine');
    expect(diarEngineLabel(t, 'future', '')).toBe('future');
  });

  it('maps each integrity index kind', () => {
    expect(integrityIndexLabel(t, 'speakers_v3', 'x')).toBe(
      'settings.dataIntegrity.indexSpeakersV3'
    );
    expect(integrityIndexLabel(t, 'speakers_v4', 'x')).toBe(
      'settings.dataIntegrity.indexSpeakersV4'
    );
    expect(integrityIndexLabel(t, 'transcripts', 'x')).toBe(
      'settings.dataIntegrity.indexTranscripts'
    );
    expect(integrityIndexLabel(t, 'summaries', 'x')).toBe('settings.dataIntegrity.indexSummaries');
    expect(integrityIndexLabel(t, undefined, 'Old backend')).toBe('Old backend');
  });

  it('maps every catalogued embedding model to keys that exist', () => {
    const ids = [
      'huggingface/sentence-transformers/all-MiniLM-L6-v2',
      'huggingface/sentence-transformers/all-MiniLM-L12-v2',
      'huggingface/sentence-transformers/multi-qa-MiniLM-L6-cos-v1',
      'huggingface/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
      'huggingface/sentence-transformers/all-mpnet-base-v2',
      'huggingface/sentence-transformers/all-distilroberta-v1',
      'huggingface/sentence-transformers/distiluse-base-multilingual-cased-v1',
    ];
    for (const id of ids) {
      for (const field of ['name', 'description'] as const) {
        const key = embeddingModelText(t, id, field, 'server text');
        expect(key).not.toBe('server text');
        expect(english[key], key).toBeTruthy();
      }
    }
    expect(embeddingModelText(t, 'huggingface/x/new-model', 'name', 'New')).toBe('New');
  });

  it('maps each statistics model purpose', () => {
    expect(modelPurposeLabel(t, 'whisper', 'x')).toBe('settings.statistics.modelPurpose.whisper');
    expect(modelPurposeLabel(t, 'search_embedding', 'x')).toBe(
      'settings.statistics.modelPurpose.searchEmbedding'
    );
    expect(modelPurposeLabel(t, 'reranker', 'Reranking')).toBe('Reranking');
  });
});

describe('local Whisper model description', () => {
  it('builds the text from the VRAM hint instead of the English sentence', () => {
    expect(
      localWhisperDescription(t, { description: '~6 GB VRAM (estimate)', vram_hint_gb: 6 })
    ).toBe('settings.localModels.vram {"gb":6}');
    expect(
      localWhisperDescription(t, { description: 'x', vram_hint_gb: 6, is_default: true })
    ).toBe('settings.localModels.vramDefault {"gb":6}');
  });

  it('falls back to the server description without a hint', () => {
    expect(localWhisperDescription(t, { description: 'English-only Whisper model' })).toBe(
      'English-only Whisper model'
    );
    expect(localWhisperDescription(t, null)).toBe('');
  });
});
