// Search results (/search, /search/semantic) carry `snippet` and
// `preview` as escaped HTML whose only markup is <mark>…</mark> around
// keyword matches (backend/routes/search.py). Split that into text runs
// and let React render them as text, rather than handing the string to
// the DOM as HTML: stored text includes what other people wrote
// (bookmarked tweets, clipped pages), and if a tag ever arrived
// unescaped it would show as literal text instead of running (#445).

const ENTITIES = {
  '&amp;': '&',
  '&lt;': '<',
  '&gt;': '>',
  '&quot;': '"',
  '&#x27;': "'",
  '&#39;': "'",
};

// One pass, so "&amp;lt;" decodes to "&lt;" and not on to "<".
export function decodeEntities(s) {
  return s.replace(/&(?:amp|lt|gt|quot|#x27|#39);/g, (m) => ENTITIES[m]);
}

// "a <mark>b</mark> c" -> [{text: 'a ', marked: false}, {text: 'b', marked: true}, ...]
export function highlightRuns(fragment) {
  const runs = [];
  let marked = false;
  for (const part of (fragment || '').split(/(<mark>|<\/mark>)/)) {
    if (part === '<mark>') marked = true;
    else if (part === '</mark>') marked = false;
    else if (part) runs.push({ text: decodeEntities(part), marked });
  }
  return runs;
}
