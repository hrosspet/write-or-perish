// Saved references (#232): shared bits for the References log and the
// reference page. A reference is not a node — it is content the user
// saved elsewhere — but it is shown with the Log's card so the two
// surfaces read as one family.

export const SOURCE_LABEL = {
  web_clip: 'Page',
  twitter_bookmark: 'Tweet',
  community_archive: 'Archive tweet',
};

export function sourceLabel(item) {
  return SOURCE_LABEL[item.source] || item.source;
}

// Tweets carry an @handle; a page's author is a byline or the site.
export function authorLabel(item) {
  if (!item.author_handle) return null;
  return item.source === 'web_clip' ? item.author_handle : `@${item.author_handle}`;
}

// Bubble reads a node; hand it a reference in node clothing. The title
// takes the thread-name slot so the first line of the text stays in the
// body (a tweet has no title and its first line is the card heading).
export function asCardNode(item) {
  return {
    id: item.id,
    preview: item.preview || '',
    thread_name: item.title || '',
    created_at: item.posted_at || item.fetched_at,
    child_count: 0,
  };
}

// The extension stores the article title separately AND Readability often
// keeps it as the first heading of the text. Show it once: drop a leading
// heading line that repeats the title.
export function bodyWithoutTitle(item) {
  const content = item.content || '';
  if (!item.title) return content;
  const m = content.match(/^\s*#{1,6}\s+(.+?)\s*\n?/);
  if (m && m[1].trim().toLowerCase() === item.title.trim().toLowerCase()) {
    return content.slice(m[0].length);
  }
  return content;
}

// Both tweet sources key items by tweet id.
export function tweetId(item) {
  if (item.source !== 'twitter_bookmark' && item.source !== 'community_archive') return null;
  return item.external_id || null;
}
