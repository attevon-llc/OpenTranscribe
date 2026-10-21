/**
 * Accent-dropped words in the Latin-script locales (issue #1222).
 *
 * The `reprocess.*`, `llm.bedrockInfo`, `settings.speakerAttributes.*` and a few other keys were
 * written with the diacritics stripped ("transcripcion", "Parametres", "nao", "Uberprufen"). They
 * are different words, not a style choice, and `check:i18n` (key parity) cannot see them. This
 * is a deny-list of words that are never correct unaccented in that language's UI copy, so it has
 * no false positives on legitimate unaccented neighbours ("transcripciones", "esta", "solo").
 *
 * A token counts only when it stands alone: not inside a `{{placeholder}}`, an identifier
 * (`pyannote.ai`, `Gender-Detection`) or a path. The scan is unit-tested against a seeded
 * sample so an over-narrow pattern cannot read as a clean tree.
 */
import { describe, it, expect } from 'vitest';
import es from './locales/es.json';
import fr from './locales/fr.json';
import pt from './locales/pt.json';
import de from './locales/de.json';

type Locale = Record<string, string>;

const DENY: Record<string, string[]> = {
  es: (
    'administracion acusticas analiticas automatico busqueda caracteristicas coleccion configuracion ' +
    'conversacion demograficos deteccion diarizacion estadisticas extraccion identificacion ' +
    'indexacion metricas migracion precision revision semantica traduccion transcripcion ' +
    'perderan reemplazara usaran desactivara rapida pagina region atras indice genero mas'
  ).split(' '),
  fr: (
    'echec etapes etat modele necessite parametres predire predits previsualisation regenerer ' +
    'reindexation reinitialiser systeme detection preserve'
  ).split(' '),
  pt: (
    'acao acusticas administracao analiticos atribuicoes automatico cartoes colecao ' +
    'configuracao configuracoes conversacao correspondencia decisoes demograficos detecao ' +
    'deteccao diarizacao estatisticas extracao identificacao indexacao marcacoes metricas ' +
    'migracao nao padrao padroes pagina precisao previsoes proximo rapido regiao regeneracao ' +
    'revisao secao selecoes sera serao substituira topicos traducao transcricao usarao ' +
    'usuarios visualizacao sao obtem preve etaria genero'
  ).split(' '),
  de: (
    'ausfuhren ausfuhrung auszufuhrende ausgewaehltem fuer schliessen schluessel ' +
    'ubereinstimmung uberprufen uberprufung uebersetzung unterstuetzt verknupfte vollstandige ' +
    'waehlen wahlen zuruck'
  ).split(' '),
};

// Phrases where the bare word is legitimate elsewhere but wrong here.
const DENY_PHRASES: Record<string, RegExp[]> = {
  es: [/\besta (activado|configurado|habilitado)\b/i],
  fr: [/\bResume IA\b/, /\bModèle demande\b/],
  pt: [/\besta configurado\b/i, /\bpre-visualizacao\b/i, /\bsecao\(oes\)/i],
  de: [/\berhalt v4\b/],
};

const BY_LOCALE: Record<string, Locale> = {
  es: es as Locale,
  fr: fr as Locale,
  pt: pt as Locale,
  de: de as Locale,
};

const strip = (s: string) => s.normalize('NFD').replace(/\p{M}/gu, '');

/** Keys whose value has a deny-listed unaccented word standing on its own. */
function offenders(locale: Locale, deny: string[], phrases: RegExp[]): string[] {
  const denySet = new Set(deny);
  const found: string[] = [];
  for (const [key, value] of Object.entries(locale)) {
    if (typeof value !== 'string') continue;
    const text = value.replace(/\{\{[^}]*\}\}/g, ' ').replace(/https?:\/\/\S+/g, ' ');
    const token = /(?<![\p{L}\p{N}_./])([\p{L}]+)(?![\p{L}\p{N}_/]|\.\p{L})/gu;
    let hit = false;
    for (const m of text.matchAll(token)) {
      // `Gender-Detection` is a proper name, `Re-transcripcion` is a prefixed word.
      if (/(?:\p{Lu}[\p{L}]*-){2,}$/u.test(text.slice(0, m.index)) && /^\p{Lu}/u.test(m[1]))
        continue;
      const lower = m[1].toLowerCase();
      if (lower === strip(lower) && denySet.has(lower)) {
        hit = true;
        break;
      }
    }
    if (hit || phrases.some((p) => p.test(text))) found.push(key);
  }
  return found;
}

describe('accent-dropped words (#1222)', () => {
  it.each(Object.keys(BY_LOCALE))('%s has no deny-listed unaccented word', (loc) => {
    expect(offenders(BY_LOCALE[loc], DENY[loc], DENY_PHRASES[loc] ?? [])).toEqual([]);
  });

  it('scan fires on a seeded sample and ignores identifiers and placeholders', () => {
    const sample: Locale = {
      bad: 'La configuracion del sistema',
      badCap: 'Transcripcion',
      okIdentifier: 'Common-Voice-Gender-Deteccion, pyannote.ai y /api/deteccion',
      badPrefixed: 'Re-transcripcion completa',
      okPlaceholder: 'Valor {{configuracion}}',
      okPlural: 'Las transcripciones y esta página',
      badSentenceEnd: 'Guardado en la configuracion.',
    };
    expect(offenders(sample, DENY.es, [])).toEqual([
      'bad',
      'badCap',
      'badPrefixed',
      'badSentenceEnd',
    ]);
  });
});
