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
 * Fenced code blocks are left alone.
 */

// "[", whitespace, ONE image, whitespace, "](href)". The alt excludes "[" so
// a run of "[![" cannot make the scan quadratic. The match is repaired only
// when a blank line sits inside the brackets (see below).
const IMAGE_LINK =
  /\[(\s*)(!\[[^[\]\n]*\]\([^()\s]+(?:[ \t]+"[^"\n]*")?\))(\s*)\]\(([^()\s]+)\)/g;
const BLANK_LINE = /\n[^\S\n]*\n/;

// Only the broken shape has a blank line inside the link text. With single
// newlines CommonMark already parses the link, and a blank line also means
// the "[" cannot sit in an inline code span, so no inline-code skipping is
// needed.
function repair(segment) {
  return segment.replace(IMAGE_LINK, (match, before, image, after, href) =>
    (BLANK_LINE.test(before) || BLANK_LINE.test(after) ? `[${image}](${href})` : match));
}

const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})/;
const FENCE_CLOSE = /^ {0,3}(`{3,}|~{3,})[ \t]*\r?\n?$/;

export function normalizeLinkedImages(text) {
  if (typeof text !== 'string' || !text.includes('](')) return text;
  if (!/\[\s*!\[/.test(text)) return text;
  // One pass over the lines; a fenced block (closed by a fence of the same
  // character and at least the same length, or the end of the text) is
  // copied as it is.
  let out = '';
  let segStart = 0;
  let fence = null;
  let pos = 0;
  while (pos < text.length) {
    const nl = text.indexOf('\n', pos);
    const end = nl < 0 ? text.length : nl + 1;
    const line = text.slice(pos, end);
    if (!fence) {
      const m = FENCE_OPEN.exec(line);
      if (m) {
        out += repair(text.slice(segStart, pos));
        segStart = pos;
        fence = m[1];
      }
    } else {
      const m = FENCE_CLOSE.exec(line);
      if (m && m[1][0] === fence[0] && m[1].length >= fence.length) {
        out += text.slice(segStart, end);
        segStart = end;
        fence = null;
      }
    }
    pos = end;
  }
  out += fence ? text.slice(segStart) : repair(text.slice(segStart));
  return out;
}
