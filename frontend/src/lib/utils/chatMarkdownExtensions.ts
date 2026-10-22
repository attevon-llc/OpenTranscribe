/**
 * `marked` extensions for assistant answers: highlighted code fences and TeX math.
 *
 * Everything returned here is still run through `chatMarkdown.ts`'s DOMPurify profile —
 * these produce markup, they do not get to bypass the sanitizer.
 *
 * Math is rendered by KaTeX as **MathML only** (`output: 'mathml'`): browsers lay MathML
 * out natively, so no KaTeX stylesheet, web fonts or `style` attributes are needed, and the
 * sanitizer never has to allow `style`. KaTeX is imported lazily because it is most of the
 * math cost and most answers contain none; until it lands, a math span shows its TeX source.
 */

import type { MarkedExtension, Tokens } from 'marked';
import hljs from 'highlight.js/lib/core';
import bash from 'highlight.js/lib/languages/bash';
import c from 'highlight.js/lib/languages/c';
import cpp from 'highlight.js/lib/languages/cpp';
import csharp from 'highlight.js/lib/languages/csharp';
import css from 'highlight.js/lib/languages/css';
import diff from 'highlight.js/lib/languages/diff';
import dockerfile from 'highlight.js/lib/languages/dockerfile';
import go from 'highlight.js/lib/languages/go';
import ini from 'highlight.js/lib/languages/ini';
import java from 'highlight.js/lib/languages/java';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import kotlin from 'highlight.js/lib/languages/kotlin';
import markdown from 'highlight.js/lib/languages/markdown';
import php from 'highlight.js/lib/languages/php';
import plaintext from 'highlight.js/lib/languages/plaintext';
import python from 'highlight.js/lib/languages/python';
import r from 'highlight.js/lib/languages/r';
import ruby from 'highlight.js/lib/languages/ruby';
import rust from 'highlight.js/lib/languages/rust';
import shell from 'highlight.js/lib/languages/shell';
import sql from 'highlight.js/lib/languages/sql';
import swift from 'highlight.js/lib/languages/swift';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml';
import yaml from 'highlight.js/lib/languages/yaml';

import { escapeHtml } from './sanitizeHtml';

// A curated set rather than the full ~190-language bundle: these cover what an answer about
// transcripts, data or code realistically contains, at a fraction of the size.
const LANGUAGES = {
  bash,
  c,
  cpp,
  csharp,
  css,
  diff,
  dockerfile,
  go,
  ini,
  java,
  javascript,
  json,
  kotlin,
  markdown,
  php,
  plaintext,
  python,
  r,
  ruby,
  rust,
  shell,
  sql,
  swift,
  typescript,
  xml,
  yaml,
};
for (const [name, definition] of Object.entries(LANGUAGES)) {
  if (!hljs.getLanguage(name)) hljs.registerLanguage(name, definition);
}

const LANGUAGE_ALIASES: Record<string, string> = { 'c++': 'cpp', 'c#': 'csharp' };

/** The fence's info-string language, normalised to something safe for a class name. */
export function fenceLanguage(info: string | undefined): string {
  const first = (info ?? '').trim().split(/\s+/)[0].toLowerCase();
  const aliased = LANGUAGE_ALIASES[first] ?? first;
  return aliased.replace(/[^\w-]/g, '');
}

/**
 * Highlight only when the language is named and known. Auto-detection is deliberately not
 * used: on a streaming buffer it re-guesses every tick, so a block would flip colour
 * schemes as it grows, and it costs a parse per registered language.
 */
function renderCode({ text, lang }: Tokens.Code): string {
  const language = fenceLanguage(lang);
  let body = escapeHtml(text);
  if (language && hljs.getLanguage(language)) {
    try {
      body = hljs.highlight(text, { language, ignoreIllegals: true }).value;
    } catch {
      // A grammar error must never lose the code; the escaped text stands.
    }
  }
  const cls = language ? `hljs language-${language}` : 'hljs';
  return `<pre><code class="${cls}">${body}\n</code></pre>\n`;
}

// ------------------------------------------------------------------------------- math

type KatexModule = typeof import('katex');

let katex: KatexModule['default'] | null = null;
let katexLoading: Promise<void> | null = null;

/** Load KaTeX once. Resolves when math can be rendered; safe to call repeatedly. */
export function loadChatMath(): Promise<void> {
  if (katex) return Promise.resolve();
  katexLoading ??= import('katex').then((mod) => {
    katex = mod.default;
  });
  return katexLoading;
}

export function isMathLoaded(): boolean {
  return katex !== null;
}

interface MathToken extends Tokens.Generic {
  type: 'chatMath';
  raw: string;
  text: string;
  display: boolean;
  block?: boolean;
}

function renderMath(token: MathToken): string {
  const fallback = `<code>${escapeHtml(token.raw.trim())}</code>`;
  if (!katex) {
    void loadChatMath();
    return token.display ? `<p>${fallback}</p>\n` : fallback;
  }
  let mathml: string;
  try {
    mathml = katex.renderToString(token.text, {
      displayMode: token.display,
      output: 'mathml',
      throwOnError: true,
      strict: 'ignore',
      // `trust: false` keeps \href, \url, \htmlClass and friends inert.
      trust: false,
      // Bound the work a hostile or runaway expression can cause.
      maxExpand: 1000,
      maxSize: 50,
    });
  } catch {
    // Invalid TeX (or a mid-stream fragment that happened to close) shows its source.
    return token.display ? `<p>${fallback}</p>\n` : fallback;
  }
  // The sanitizer drops <semantics>/<annotation> (DOMPurify classes them as unsafe MathML)
  // but KEEPS their text, which would print the TeX source inside the formula. Unwrap them
  // here and carry the source as `alttext`, which assistive tech reads.
  mathml = mathml
    .replace(/<annotation encoding="application\/x-tex">[\s\S]*?<\/annotation>/g, '')
    .replace(/<\/?semantics>/g, '')
    .replace(/<math\b/, `<math alttext="${escapeHtml(token.text)}"`);
  return token.display
    ? `<span class="chat-math-display">${mathml}</span>${token.block ? '\n' : ''}`
    : `<span class="chat-math">${mathml}</span>`;
}

// Block: `$$` or `\[` opening a line, closed by `$$` / `\]` ending a line.
const BLOCK_DOLLAR = /^ {0,3}\$\$([\s\S]+?)\$\$[ \t]*(?:\n+|$)/;
const BLOCK_BRACKET = /^ {0,3}\\\[([\s\S]+?)\\\][ \t]*(?:\n+|$)/;

// Inline. Single-dollar math follows the pandoc rule that keeps currency out: the opening
// `$` is not followed by whitespace, the closing one is not preceded by whitespace and not
// followed by a digit. "$5 and $10" and "$5-$10" therefore stay text.
const INLINE_RULES: Array<[RegExp, boolean]> = [
  [/^\$\$([^$]+?)\$\$/, true],
  [/^\\\[([\s\S]+?)\\\]/, true],
  [/^\\\(([\s\S]+?)\\\)/, false],
  [/^\$(?![\s$])((?:\\.|[^\\$\n])+?)(?<![\s\\])\$(?!\d)/, false],
];

export const chatMarkdownExtensions: MarkedExtension = {
  renderer: { code: renderCode },
  extensions: [
    {
      name: 'chatMathBlock',
      level: 'block',
      start: (src) => src.match(/^ {0,3}(\$\$|\\\[)/m)?.index,
      tokenizer(src) {
        const match = BLOCK_DOLLAR.exec(src) ?? BLOCK_BRACKET.exec(src);
        if (!match || !match[1].trim()) return undefined;
        return {
          type: 'chatMath',
          raw: match[0],
          text: match[1].trim(),
          display: true,
          block: true,
        };
      },
    },
    {
      name: 'chatMath',
      level: 'inline',
      start: (src) => src.match(/\$|\\[([]/)?.index,
      tokenizer(src) {
        for (const [rule, display] of INLINE_RULES) {
          const match = rule.exec(src);
          if (match && match[1].trim()) {
            return { type: 'chatMath', raw: match[0], text: match[1].trim(), display };
          }
        }
        return undefined;
      },
      renderer: (token) => renderMath(token as MathToken),
    },
  ],
};
