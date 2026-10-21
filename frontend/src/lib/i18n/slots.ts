/**
 * Put a link, badge or other markup INSIDE a translated sentence.
 *
 * Copy such as "licensed under the <a>GNU AGPL</a>." used to be three keys glued together
 * (`licensedUnder` + link + `licensedUnderSuffix`), which cannot be translated for a language
 * that puts the link first or the verb last. The translator now gets one sentence with a named
 * placeholder, and the component asks i18next to interpolate {@link SLOT} for it and splits the
 * result around it, rendering the markup where the translator placed the placeholder.
 *
 * The slot is a private-use code point so it survives i18next's HTML escaping and can never
 * collide with real text.
 */
export const SLOT = '\uE000';

/**
 * Split a sentence already interpolated with {@link SLOT} into the text before and after it.
 *
 * If a translation dropped the placeholder the slot is absent: return the full sentence and an
 * empty tail, so the markup still renders (after the text) instead of vanishing.
 */
export function splitAtSlot(text: string): [string, string] {
  const at = text.indexOf(SLOT);
  if (at === -1) return [`${text} `, ''];
  return [text.slice(0, at), text.slice(at + SLOT.length)];
}
