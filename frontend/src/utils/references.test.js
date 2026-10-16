import {
  authorLabel, bodyWithoutTitle, referenceKind, sourceLabel, sourceLine, sourceLinkLabel, youtubeVideo,
} from './references';

const clip = (url) => ({ source: 'web_clip', url });

describe('youtubeVideo', () => {
  it('reads a watch URL', () => {
    expect(youtubeVideo(clip('https://www.youtube.com/watch?v=dQw4w9WgXcQ')))
      .toEqual({ id: 'dQw4w9WgXcQ', start: 0 });
  });
  it('reads youtu.be, shorts, live and embed paths', () => {
    expect(youtubeVideo(clip('https://youtu.be/dQw4w9WgXcQ')).id).toBe('dQw4w9WgXcQ');
    expect(youtubeVideo(clip('https://www.youtube.com/shorts/dQw4w9WgXcQ')).id).toBe('dQw4w9WgXcQ');
    expect(youtubeVideo(clip('https://www.youtube.com/live/dQw4w9WgXcQ?feature=share')).id).toBe('dQw4w9WgXcQ');
    expect(youtubeVideo(clip('https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1')).id).toBe('dQw4w9WgXcQ');
  });
  it('carries the start time over', () => {
    expect(youtubeVideo(clip('https://youtu.be/dQw4w9WgXcQ?t=90')).start).toBe(90);
    expect(youtubeVideo(clip('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1h2m3s')).start).toBe(3723);
    expect(youtubeVideo(clip('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=45s')).start).toBe(45);
  });
  it('is null for channels, playlists, other sites, tweets and bad ids', () => {
    expect(youtubeVideo(clip('https://www.youtube.com/@artofaccomplishment'))).toBeNull();
    expect(youtubeVideo(clip('https://www.youtube.com/playlist?list=PL1'))).toBeNull();
    expect(youtubeVideo(clip('https://www.youtube.com/watch?v=short'))).toBeNull();
    expect(youtubeVideo(clip('https://notyoutube.com/watch?v=dQw4w9WgXcQ'))).toBeNull();
    expect(youtubeVideo(clip('not a url'))).toBeNull();
    expect(youtubeVideo({ source: 'twitter_bookmark', url: 'https://x.com/i/status/1' })).toBeNull();
    expect(youtubeVideo({ source: 'web_clip', url: null })).toBeNull();
  });
});

describe('sourceLabel', () => {
  it('labels a YouTube clip as a video and other clips as pages', () => {
    expect(sourceLabel(clip('https://www.youtube.com/watch?v=dQw4w9WgXcQ'))).toBe('Video');
    expect(sourceLabel(clip('https://example.com/post'))).toBe('Page');
    expect(sourceLabel({ source: 'twitter_bookmark' })).toBe('Tweet');
    expect(sourceLabel({ source: 'twitter_like' })).toBe('Tweet');
  });
});

describe('referenceKind', () => {
  it('tells videos, tweets and pages apart', () => {
    expect(referenceKind(clip('https://youtu.be/dQw4w9WgXcQ'))).toBe('video');
    expect(referenceKind(clip('https://www.youtube.com/@channel'))).toBe('page');
    ['community_archive', 'read_pick', 'twitter_bookmark', 'twitter_like'].forEach((source) => {
      expect(referenceKind({ source })).toBe('tweet');
    });
    expect(referenceKind(clip('https://example.com/post'))).toBe('page');
    expect(referenceKind({ source: 'new_source' })).toBe('page');
  });
});

describe('sourceLine', () => {
  it('names the kind and where it came from in a plain sentence', () => {
    expect(sourceLine({ source: 'community_archive' })).toBe('Tweet from the Community Archive');
    expect(sourceLine({ source: 'read_pick' })).toBe('Tweet from the Community Archive');
    expect(sourceLine({ source: 'twitter_bookmark' })).toBe('Tweet from your Twitter bookmarks');
    expect(sourceLine({ source: 'twitter_like' })).toBe('Tweet from your Twitter likes');
    expect(sourceLine(clip('https://www.youtube.com/watch?v=dQw4w9WgXcQ'))).toBe('Video from YouTube');
    expect(sourceLine(clip('https://www.example.com/post'))).toBe('Page from example.com');
    expect(sourceLine(clip(null))).toBe('Saved page');
    expect(sourceLine({ source: 'new_source' })).toBe('new_source');
  });
  it('names an uploaded document (a PDF saved as someone else\'s writing)', () => {
    expect(sourceLine({ source: 'document', url: null })).toBe('Document you uploaded');
    expect(sourceLabel({ source: 'document' })).toBe('Document');
    expect(referenceKind({ source: 'document' })).toBe('page');
  });
});

describe('authorLabel', () => {
  it('shows a byline as it is and a tweet author with an @', () => {
    expect(authorLabel({ source: 'document', author_handle: 'Jane Doe' })).toBe('Jane Doe');
    expect(authorLabel({ source: 'web_clip', author_handle: 'example.com' })).toBe('example.com');
    expect(authorLabel({ source: 'twitter_bookmark', author_handle: 'visa' })).toBe('@visa');
    expect(authorLabel({ source: 'document', author_handle: null })).toBeNull();
  });
});

describe('sourceLinkLabel', () => {
  it('names where the link out goes', () => {
    expect(sourceLinkLabel({ source: 'twitter_bookmark' })).toBe('Twitter ↗');
    expect(sourceLinkLabel(clip('https://youtu.be/dQw4w9WgXcQ'))).toBe('YouTube ↗');
    expect(sourceLinkLabel(clip('https://example.com/post'))).toBe('Open page ↗');
  });
});

describe('bodyWithoutTitle', () => {
  it('drops a leading heading that repeats the title', () => {
    const item = { title: 'Edit and TTS test', content: '# Edit and TTS test\n\nBody text.' };
    expect(bodyWithoutTitle(item)).toBe('\nBody text.');
  });
  it('keeps a heading that differs from the title, and everything without a title', () => {
    expect(bodyWithoutTitle({ title: 'Other', content: '# A heading\n\nBody.' })).toBe('# A heading\n\nBody.');
    expect(bodyWithoutTitle({ title: null, content: '# A heading\n\nBody.' })).toBe('# A heading\n\nBody.');
  });
});
