import XCTest
@testable import Loore

/// Port of `utils/references.test.js`, plus cases for the other helpers.
final class ReferenceUtilsTests: XCTestCase {
    private func clip(_ url: String?) -> ReferenceUtils.YouTubeVideo? {
        ReferenceUtils.youtubeVideo(source: "web_clip", url: url)
    }

    // MARK: youtubeVideo

    func testReadsAWatchURL() {
        XCTAssertEqual(clip("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
                       ReferenceUtils.YouTubeVideo(id: "dQw4w9WgXcQ", start: 0))
    }

    func testReadsYoutuBeShortsLiveAndEmbedPaths() {
        XCTAssertEqual(clip("https://youtu.be/dQw4w9WgXcQ")?.id, "dQw4w9WgXcQ")
        XCTAssertEqual(clip("https://www.youtube.com/shorts/dQw4w9WgXcQ")?.id, "dQw4w9WgXcQ")
        XCTAssertEqual(clip("https://www.youtube.com/live/dQw4w9WgXcQ?feature=share")?.id, "dQw4w9WgXcQ")
        XCTAssertEqual(clip("https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1")?.id, "dQw4w9WgXcQ")
    }

    func testCarriesTheStartTimeOver() {
        XCTAssertEqual(clip("https://youtu.be/dQw4w9WgXcQ?t=90")?.start, 90)
        XCTAssertEqual(clip("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1h2m3s")?.start, 3723)
        XCTAssertEqual(clip("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=45s")?.start, 45)
    }

    func testIsNilForChannelsPlaylistsOtherSitesTweetsAndBadIds() {
        XCTAssertNil(clip("https://www.youtube.com/@artofaccomplishment"))
        XCTAssertNil(clip("https://www.youtube.com/playlist?list=PL1"))
        XCTAssertNil(clip("https://www.youtube.com/watch?v=short"))
        XCTAssertNil(clip("https://notyoutube.com/watch?v=dQw4w9WgXcQ"))
        XCTAssertNil(clip("not a url"))
        XCTAssertNil(ReferenceUtils.youtubeVideo(source: "twitter_bookmark", url: "https://x.com/i/status/1"))
        XCTAssertNil(clip(nil))
    }

    // MARK: sourceLabel

    func testLabelsAYouTubeClipAsAVideoAndOtherClipsAsPages() {
        XCTAssertEqual(ReferenceUtils.sourceLabel(source: "web_clip", url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
                       "Video")
        XCTAssertEqual(ReferenceUtils.sourceLabel(source: "web_clip", url: "https://example.com/post"), "Page")
        XCTAssertEqual(ReferenceUtils.sourceLabel(source: "twitter_bookmark", url: nil), "Tweet")
    }

    // MARK: bodyWithoutTitle

    func testDropsALeadingHeadingThatRepeatsTheTitle() {
        XCTAssertEqual(ReferenceUtils.bodyWithoutTitle(title: "Edit and TTS test",
                                                       content: "# Edit and TTS test\n\nBody text."),
                       "\nBody text.")
    }

    func testKeepsAHeadingThatDiffersFromTheTitleAndEverythingWithoutATitle() {
        XCTAssertEqual(ReferenceUtils.bodyWithoutTitle(title: "Other", content: "# A heading\n\nBody."),
                       "# A heading\n\nBody.")
        XCTAssertEqual(ReferenceUtils.bodyWithoutTitle(title: nil, content: "# A heading\n\nBody."),
                       "# A heading\n\nBody.")
    }

    // MARK: Extra cases (outputs checked against the web)

    func testYouTubeEdgeCases() {
        XCTAssertEqual(clip("https://WWW.YouTube.COM/watch?v=dQw4w9WgXcQ")?.id, "dQw4w9WgXcQ")
        XCTAssertEqual(clip("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?start=12"),
                       ReferenceUtils.YouTubeVideo(id: "dQw4w9WgXcQ", start: 12))
        XCTAssertEqual(clip("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=&start=30")?.start, 30)
        XCTAssertEqual(clip("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1m1h")?.start, 0)
        XCTAssertNil(clip("https://www.youtube.com:abc/watch?v=dQw4w9WgXcQ"))
        XCTAssertNil(clip("https://www.youtube.com/Watch?v=dQw4w9WgXcQ"))
        XCTAssertNil(clip("youtube.com/watch?v=dQw4w9WgXcQ"))
    }

    func testStartSeconds() {
        XCTAssertEqual(ReferenceUtils.startSeconds(nil), 0)
        XCTAssertEqual(ReferenceUtils.startSeconds("007"), 7)
        XCTAssertEqual(ReferenceUtils.startSeconds("2m"), 120)
        XCTAssertEqual(ReferenceUtils.startSeconds("1h"), 3600)
        XCTAssertEqual(ReferenceUtils.startSeconds("1h2m3s4"), 0)
        XCTAssertEqual(ReferenceUtils.startSeconds("٣"), 0)  // JS \d is ASCII only
    }

    func testBodyWithoutTitleEdgeCases() {
        XCTAssertEqual(ReferenceUtils.bodyWithoutTitle(title: "t", content: "  \n\n### T  \nrest"), "rest")
        XCTAssertEqual(ReferenceUtils.bodyWithoutTitle(title: "T", content: "# T\r\n"), "")
        XCTAssertEqual(ReferenceUtils.bodyWithoutTitle(title: "T", content: "#T\nrest"), "#T\nrest")
        XCTAssertEqual(ReferenceUtils.bodyWithoutTitle(title: "T", content: nil), "")
    }

    // MARK: The reference page's card (option A, 2026-10-13)

    func testKindTellsVideosTweetsAndPagesApart() {
        XCTAssertEqual(ReferenceUtils.kind(source: "web_clip", url: "https://youtu.be/dQw4w9WgXcQ"), .video)
        XCTAssertEqual(ReferenceUtils.kind(source: "web_clip", url: "https://www.youtube.com/@channel"), .page)
        for source in ["community_archive", "read_pick", "twitter_bookmark", "twitter_like"] {
            XCTAssertEqual(ReferenceUtils.kind(source: source, url: nil), .tweet, source)
        }
        XCTAssertEqual(ReferenceUtils.kind(source: "web_clip", url: "https://example.com/post"), .page)
        XCTAssertEqual(ReferenceUtils.kind(source: "new_source", url: nil), .page)
    }

    func testSourceLineNamesTheKindAndWhereItCameFrom() {
        XCTAssertEqual(ReferenceUtils.sourceLine(source: "community_archive", url: nil), "Tweet from the Community Archive")
        XCTAssertEqual(ReferenceUtils.sourceLine(source: "read_pick", url: nil), "Tweet from the Community Archive")
        XCTAssertEqual(ReferenceUtils.sourceLine(source: "twitter_bookmark", url: nil), "Tweet from your Twitter bookmarks")
        XCTAssertEqual(ReferenceUtils.sourceLine(source: "twitter_like", url: nil), "Tweet from your Twitter likes")
        XCTAssertEqual(ReferenceUtils.sourceLine(source: "web_clip", url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
                       "Video from YouTube")
        XCTAssertEqual(ReferenceUtils.sourceLine(source: "web_clip", url: "https://www.example.com/post"),
                       "Page from example.com")
        XCTAssertEqual(ReferenceUtils.sourceLine(source: "web_clip", url: nil), "Saved page")
        XCTAssertEqual(ReferenceUtils.sourceLine(source: "new_source", url: nil), "new_source")
    }

    func testSourceLinkLabelNamesWhereTheLinkGoes() {
        XCTAssertEqual(ReferenceUtils.sourceLinkLabel(source: "twitter_bookmark", url: nil), "Twitter ↗")
        XCTAssertEqual(ReferenceUtils.sourceLinkLabel(source: "web_clip", url: "https://youtu.be/dQw4w9WgXcQ"), "YouTube ↗")
        XCTAssertEqual(ReferenceUtils.sourceLinkLabel(source: "web_clip", url: "https://example.com/post"), "Open page ↗")
    }

    func testAuthorLabelAndCardFields() {
        XCTAssertEqual(ReferenceUtils.authorLabel(source: "twitter_bookmark", handle: "peter"), "@peter")
        XCTAssertEqual(ReferenceUtils.authorLabel(source: "web_clip", handle: "The Site"), "The Site")
        XCTAssertNil(ReferenceUtils.authorLabel(source: "twitter_bookmark", handle: ""))
        XCTAssertEqual(ReferenceUtils.sourceLabel(source: "twitter_like", url: nil), "Tweet")
        XCTAssertEqual(ReferenceUtils.sourceLabel(source: "community_archive", url: nil), "Archive tweet")
        XCTAssertEqual(ReferenceUtils.sourceLabel(source: "new_source", url: nil), "new_source")

        let fetched = Date(timeIntervalSince1970: 1_700_000_000)
        XCTAssertEqual(ReferenceUtils.asCardNode(id: 7, preview: nil, title: nil, postedAt: nil, fetchedAt: fetched),
                       ReferenceUtils.CardFields(id: 7, preview: "", threadName: "", createdAt: fetched, childCount: 0))
        let posted = Date(timeIntervalSince1970: 1_600_000_000)
        XCTAssertEqual(ReferenceUtils.asCardNode(id: 7, preview: "p", title: "T", postedAt: posted, fetchedAt: fetched)
            .createdAt, posted)
    }
}
