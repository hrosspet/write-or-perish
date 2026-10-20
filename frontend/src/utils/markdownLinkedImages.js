/**
 * Repairs the shape the Chrome clipper used to save for a linked image
 * (Substack wraps every image in a link around block content):
 *
 *   [
 *
 *   ![alt](src)
 *
 *   ](href)
 *
 * A blank line inside link text breaks the link in CommonMark, so it showed
 * as a stray "[", the image, and a raw "](href)". Rewritten to
 * `[![alt](src)](href)`. The clipper no longer produces it (extension 0.1.2),
 * but saved References keep their stored text, which is encrypted at rest, so
 * the repair happens when the text is rendered. iOS has the same function
 * (MarkdownLinkedImages.swift); keep the two in step.
 *
 * Narrow on purpose: only "[", whitespace, ONE image, whitespace, "](href)".
 * Fenced code blocks and inline code spans are left alone.
 */

const BROKEN_IMAGE_LINK =
  /\[\s*(!\[[^\]\n]*\]\([^()\s]+(?:[ \t]+"[^"\n]*")?\))\s*\]\(([^()\s]+)\)/g;

// A fenced block (``` or ~~~, closed by the same fence or the end of the
// text) or an inline code span; these are skipped.
const CODE = /^[ ]{0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^[ ]{0,3}\1[`~]*[ \t]*$|(?![\s\S]))|(`+)[^`][\s\S]*?\2(?!`)/gm;

export function normalizeLinkedImages(text) {
  if (typeof text !== 'string' || !text.includes('](')) return text;
  if (!/\[\s*!\[/.test(text)) return text;
  let out = '';
  let last = 0;
  CODE.lastIndex = 0;
  let m;
  while ((m = CODE.exec(text))) {
    out += text.slice(last, m.index).replace(BROKEN_IMAGE_LINK, '[$1]($2)');
    out += m[0];
    last = m.index + m[0].length;
    if (m[0].length === 0) CODE.lastIndex += 1;
  }
  out += text.slice(last).replace(BROKEN_IMAGE_LINK, '[$1]($2)');
  return out;
}
