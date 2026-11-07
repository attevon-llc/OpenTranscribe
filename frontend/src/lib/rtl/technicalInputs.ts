/**
 * Convention for form fields whose value is not prose (URL, host, path, DN, filter, e-mail,
 * credential, regex...): the `<input>` carries `dir="ltr"` so the bidi algorithm cannot
 * reorder it inside an RTL page, while the label and layout stay RTL.
 *
 * References: W3C "Inline markup and bidirectional text in HTML"
 * (https://www.w3.org/International/articles/inline-bidi-markup/) and MDN `dir`
 * (https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Global_attributes/dir).
 * `technicalInputs.test.ts` enforces it over the admin/settings panels.
 */

// Matched against an input's `id`. Anything not listed (names, labels, titles) is prose.
const TECHNICAL_ID =
  /url|uri|host|server|path|port|(^|[-_])dn|filter|attr|claim|realm|client|secret|token|key|session|endpoint|bucket|region|prefix|domain|header|regex|extension|(^|[-_])ext$|schedule|issuer|entity|audience|scope|group|role|proxies|separator|cert|destination|passphrase|e?mail|from|rcpt|tenant|model|s3|smb|exsrv|exdom|(^|[-_])pass(word)?$|passphrase|users$|callback|mapping-claim/i;
const PROSE_ID = /(^|[-_])(name|label|title|temperature)$/i;

const INPUT_TAG = /<(?:input|textarea)\b(?:[^>"'{]|"[^"]*"|'[^']*'|\{[^}]*\})*>/g;
const NON_TEXT_TYPE = /type="(checkbox|radio|range|file|number|date|hidden|color)"/;

/** True when the opening `<input ...>`/`<textarea ...>` tag holds a technical (non-prose) value. */
export function isTechnicalInputTag(tag: string): boolean {
  if (NON_TEXT_TYPE.test(tag)) return false;
  if (/type="(url|email)"/.test(tag)) return true;
  const id = /\bid="([^"]*)"/.exec(tag)?.[1];
  if (!id) return false;
  // `ldap_tls_server_name` is a host name, so a host/server id outranks the `_name` suffix.
  if (PROSE_ID.test(id) && !/host|server/i.test(id)) return false;
  return TECHNICAL_ID.test(id);
}

/** Opening `<input>`/`<textarea>` tags in a Svelte source that are technical but lack `dir="ltr"`. */
export function technicalInputsMissingLtr(source: string): string[] {
  return (source.match(INPUT_TAG) ?? []).filter(
    (tag) => isTechnicalInputTag(tag) && !/\bdir="ltr"/.test(tag)
  );
}
