import React from 'react';

/**
 * The video player for a saved YouTube reference, the way TweetEmbed
 * shows the real tweet. The stored text (title + description, with the
 * chapter list) stays the searchable copy and what the AI reads; this
 * is for the reader, who wants to watch.
 *
 * Privacy: opening a YouTube reference loads an iframe from
 * youtube-nocookie.com, so Google learns the viewer's IP and which
 * video was opened, but sets no tracking cookie until the viewer
 * presses play. Like the tweet embed, this happens only on a reference
 * page the user opened; the References log shows the stored text.
 *
 * Unlike X's widget, an iframe cannot tell us the video is gone (a
 * cross-origin frame fires no error), so there is no 'unavailable'
 * state: YouTube renders its own "video unavailable" card and the
 * stored text sits under the toggle.
 */
function YouTubeEmbed({ videoId, start }) {
  if (!videoId) return null;
  const params = new URLSearchParams({ rel: '0' });
  if (start) params.set('start', String(start));
  const src = `https://www.youtube-nocookie.com/embed/${videoId}?${params.toString()}`;
  return (
    <div style={{
      position: 'relative', width: '100%', maxWidth: '860px',
      aspectRatio: '16 / 9', margin: '0 auto',
      borderRadius: '8px', overflow: 'hidden', background: '#000',
    }}>
      <iframe
        src={src}
        title="YouTube video"
        loading="lazy"
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
        referrerPolicy="strict-origin-when-cross-origin"
        allowFullScreen
        style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', border: 0 }}
      />
    </div>
  );
}

export default YouTubeEmbed;
