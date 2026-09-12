import { bodyWithoutTitle, sourceLabel, youtubeVideo } from './references';

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
