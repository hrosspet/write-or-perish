import XCTest
@testable import Loore

/// Port of `utils/markdown.test.js`.
final class MarkdownEditsTests: XCTestCase {
    private let content = [
        "## Financial investments",
        "- [ ] Monitor markets",
        "- [ ] CC artifact with my [financials](https://claude.ai/code/artifact/abc?via=x)",
    ].joined(separator: "\n")
    private let rawLabel = "CC artifact with my [financials](https://claude.ai/code/artifact/abc?via=x)"

    func testStripInlineMarkdownReducesALinkToItsText() {
        XCTAssertEqual(MarkdownEdits.stripInlineMarkdown(rawLabel), "CC artifact with my financials")
    }

    func testALinkBearingItemTogglesWhenKeyedByItsStrippedLabel() {
        let out = MarkdownEdits.toggleCheckbox(content, itemText: MarkdownEdits.stripInlineMarkdown(rawLabel).jsTrimmed,
                                               currentChecked: false)
        XCTAssertEqual(out.jsLines[2], "- [x] \(rawLabel)")
        // The raw label does not match, which is why callers must strip first.
        XCTAssertEqual(MarkdownEdits.toggleCheckbox(content, itemText: rawLabel, currentChecked: false), content)
    }

    func testInsertingAfterALinkBearingItemLandsBelowIt() {
        let out = MarkdownEdits.insertItemAfter(content, afterItemText: MarkdownEdits.stripInlineMarkdown(rawLabel).jsTrimmed,
                                                newText: "New task")
        XCTAssertEqual(out.jsLines[3], "- [ ] New task")
    }

    // Extra cases for the rules the renderer depends on.

    func testStripHandlesEmphasisCodeAndStrikeLikeTheWeb() {
        XCTAssertEqual(MarkdownEdits.stripInlineMarkdown("**bold** rest"), "bold rest")
        XCTAssertEqual(MarkdownEdits.stripInlineMarkdown("an *em* and _em_ and `code` and ~~gone~~"),
                       "an em and em and code and gone")
        // Intraword asterisks/underscores are not emphasis (JS \w is ASCII only).
        XCTAssertEqual(MarkdownEdits.stripInlineMarkdown("snake_case_name"), "snake_case_name")
        XCTAssertEqual(MarkdownEdits.stripInlineMarkdown("č*x*"), "čx")
    }

    func testToggleUnchecksAndKeepsIndent() {
        let text = "- [x] one\n  - [x] two"
        XCTAssertEqual(MarkdownEdits.toggleCheckbox(text, itemText: "two", currentChecked: true), "- [x] one\n  - [ ] two")
    }

    func testInsertAfterSkipsTheNestedSubtreeAndCopiesStyle() {
        let text = "* parent\n  * child\n* next"
        XCTAssertEqual(MarkdownEdits.insertItemAfter(text, afterItemText: "parent", newText: " new "),
                       "* parent\n  * child\n* new\n* next")
    }

    func testAppendItemToSectionAppendsOrCreates() {
        XCTAssertEqual(MarkdownEdits.appendItemToSection("## Today\n- [ ] a\n\n## Later\n- [ ] b", sectionTitle: "Today",
                                                         task: "c"),
                       "## Today\n- [ ] a\n- [ ] c\n\n## Later\n- [ ] b")
        XCTAssertEqual(MarkdownEdits.appendItemToSection("## Later\n- [ ] b\n", sectionTitle: "Today", task: "c",
                                                         createAtStart: true),
                       "## Today\n\n- [ ] c\n\n## Later\n- [ ] b\n")
    }
}

/// Port of `components/MarkdownBody.test.js` (`remarkHtmlAsCode`) and
/// `MarkdownBody.stable.test.js` (#321), plus the renderer's own parsing rules.
final class MarkdownParserTests: XCTestCase {
    private func parse(_ s: String) -> [MDBlock] { MarkdownParser.parse(s).blocks }

    func testBlockLevelHTMLBecomesACodeBlockPreservingTheRawValue() {
        let value = "<user-archive>\n{user_export?days=92}\n</user-archive>"
        let blocks = parse("hello\n\n" + value)
        XCTAssertEqual(blocks.count, 2)
        XCTAssertEqual(blocks[1].kind, .code(language: nil, text: value))
    }

    func testInlineHTMLInsideAParagraphBecomesInlineCode() {
        let blocks = parse("before <user-archive>x</user-archive>")
        guard case .paragraph(let inlines) = blocks[0].kind else { return XCTFail("not a paragraph") }
        XCTAssertEqual(inlines, [.text("before "), .code("<user-archive>"), .text("x"), .code("</user-archive>")])
    }

    func testPureHTMLCommentsAreDropped() {
        let blocks = parse("<!-- id: my-feature -->\n\nbody")
        XCTAssertEqual(blocks.count, 1)
        guard case .paragraph = blocks[0].kind else { return XCTFail("not a paragraph") }
    }

    func testRecursesIntoBlockquotes() {
        let blocks = parse("> <note>\n> quoted\n> </note>")
        guard case .blockquote(let inner) = blocks[0].kind, case .code = inner.first?.kind else {
            return XCTFail("expected code inside the blockquote")
        }
    }

    func testNonHTMLIsUntouched() {
        XCTAssertEqual(parse("plain").first?.kind, .paragraph([.text("plain")]))
    }

    // #321: toggling keeps every row's identity, so rows update in place.
    func testTogglingACheckboxKeepsRowIdentities() {
        let list = { (a: String, b: String, c: String) in "- [\(a)] one\n- [\(b)] two\n- [\(c)] three" }
        guard case .list(let before) = parse(list(" ", " ", " "))[0].kind,
              case .list(let after) = parse(list(" ", "x", " "))[0].kind else { return XCTFail("not a list") }
        XCTAssertEqual(before.items.map(\.plainText), ["one", "two", "three"])
        XCTAssertEqual(before.items[1].checked, false)
        XCTAssertEqual(after.items[1].checked, true)
        XCTAssertEqual(before.items.map(\.id), after.items.map(\.id))
        // The toggle the renderer fires for row two, applied to the source.
        XCTAssertEqual(MarkdownEdits.toggleCheckbox(list(" ", " ", " "), itemText: before.items[1].plainText,
                                                    currentChecked: false), list(" ", "x", " "))
    }

    // The "+" input state lives above the rows, keyed by the row's text, so a
    // re-render with new content keeps a half-typed item.
    @MainActor
    func testAddItemStateSurvivesAReparse() {
        let state = ChecklistEditState()
        state.addingAfter = "one"
        state.draft = "half typed"
        guard case .list(let list) = parse("- [ ] one\n- [ ] two")[0].kind else { return XCTFail("not a list") }
        XCTAssertTrue(list.items.contains { $0.plainText == state.addingAfter })
        XCTAssertEqual(state.draft, "half typed")
    }

    func testTaskItemPlainTextMatchesStrippedSourceLabel() {
        let source = "- [ ] Read **the** [paper](https://x.org) with `code` and *care*\n  - [ ] nested child"
        guard case .list(let list) = parse(source)[0].kind else { return XCTFail("not a list") }
        let label = "Read **the** [paper](https://x.org) with `code` and *care*"
        XCTAssertEqual(list.items[0].plainText, MarkdownEdits.stripInlineMarkdown(label).jsTrimmed)
    }

    func testNoSmartPunctuation() {
        XCTAssertEqual(parse("\"quoted\" -- dash").first?.kind, .paragraph([.text("\"quoted\" -- dash")]))
    }

    func testSoftBreaksAreKept() {
        XCTAssertEqual(parse("line one\nline two").first?.kind,
                       .paragraph([.text("line one"), .softBreak, .text("line two")]))
    }

    func testTightAndLooseLists() {
        guard case .list(let tight) = parse("- a\n- b")[0].kind,
              case .list(let loose) = parse("- a\n\n- b")[0].kind else { return XCTFail("not lists") }
        XCTAssertTrue(tight.tight)
        XCTAssertFalse(loose.tight)
        XCTAssertFalse(tight.isTaskList)
    }

    func testTablesParse() {
        guard case .table(let table) = parse("| a | b |\n|:--|--:|\n| 1 | 2 |")[0].kind else { return XCTFail("no table") }
        XCTAssertEqual(table.head, [[.text("a")], [.text("b")]])
        XCTAssertEqual(table.rows, [[[.text("1")], [.text("2")]]])
        XCTAssertEqual(table.alignments, [.left, .right])
    }

    func testAutolinkLiterals() {
        XCTAssertEqual(parse("see https://loore.org/node/5. Next").first?.kind, .paragraph([
            .text("see "), .link(destination: "https://loore.org/node/5", children: [.text("https://loore.org/node/5")]),
            .text(". Next"),
        ]))
        XCTAssertEqual(parse("(www.example.com/a)").first?.kind, .paragraph([
            .text("("), .link(destination: "http://www.example.com/a", children: [.text("www.example.com/a")]),
            .text(")"),
        ]))
        XCTAssertEqual(parse("mail a@b.co now").first?.kind, .paragraph([
            .text("mail "), .link(destination: "mailto:a@b.co", children: [.text("a@b.co")]), .text(" now"),
        ]))
        // Not inside code, and not inside a link's text.
        XCTAssertEqual(parse("`https://x.org`").first?.kind, .paragraph([.code("https://x.org")]))
        XCTAssertEqual(parse("[https://x.org](https://y.org)").first?.kind,
                       .paragraph([.link(destination: "https://y.org", children: [.text("https://x.org")])]))
    }

    func testBareLinkDetection() {
        XCTAssertTrue(InlineAttributedBuilder.isBare([.text("https://loore.org/node/5/")], href: "https://loore.org/node/5"))
        XCTAssertFalse(InlineAttributedBuilder.isBare([.text("my entry")], href: "https://loore.org/node/5"))
    }
}

/// Port of `components/Bubble.test.js`.
final class BubblePreviewTests: XCTestCase {
    private let voiceNote = "# 2026-09-05 12:12:01 Voice note\nFirst line of the transcript."

    func testSplitPreviewStripsTheHeadingMarkerAndSeparatesBody() {
        XCTAssertEqual(BubblePreview.split("# Title\nBody line"), .init(title: "Title", body: "Body line", isHeading: true))
        XCTAssertEqual(BubblePreview.split("Just one line"), .init(title: "Just one line", body: "", isHeading: false))
        XCTAssertEqual(BubblePreview.split(""), .init(title: "", body: "", isHeading: false))
    }

    func testWithoutAThreadNameTheFirstLineIsTheTitle() {
        let d = BubblePreview.display(text: voiceNote, threadName: nil)
        XCTAssertEqual(d.heading, "2026-09-05 12:12:01 Voice note")
        XCTAssertEqual(d.body, "First line of the transcript.")
    }

    func testAThreadNameReplacesAMarkdownHeading() {
        let d = BubblePreview.display(text: voiceNote, threadName: "Teplárna plan")
        XCTAssertEqual(d.heading, "Teplárna plan")
        XCTAssertEqual(d.body, "First line of the transcript.")
    }

    func testAThreadNameOnPlainTextKeepsEveryLineAsTheBody() {
        let d = BubblePreview.display(text: "Quick thought about X\nand a second line.", threadName: "Thoughts on X")
        XCTAssertEqual(d.heading, "Thoughts on X")
        XCTAssertEqual(d.body, "Quick thought about X\nand a second line.")
    }

    func testAThreadNameOnAOneLinePlainEntryKeepsThatLineAsTheBody() {
        let d = BubblePreview.display(text: "Quick thought about X", threadName: "Thoughts on X")
        XCTAssertEqual(d.heading, "Thoughts on X")
        XCTAssertEqual(d.body, "Quick thought about X")
    }

    func testAThreadNameOnAHeadingOnlyEntryShowsJustTheName() {
        let d = BubblePreview.display(text: "# 2026-09-05 12:12:01 Voice note", threadName: "Named")
        XCTAssertEqual(d.heading, "Named")
        XCTAssertEqual(d.body, "")
    }

    func testABlankThreadNameFallsBackToTheTitle() {
        XCTAssertEqual(BubblePreview.display(text: voiceNote, threadName: "   ").heading, "2026-09-05 12:12:01 Voice note")
    }

    func testExpandAndPromptLabels() {
        XCTAssertFalse(BubblePreview.canExpand(content: nil, text: "a\nb\nc\nd"))
        XCTAssertTrue(BubblePreview.canExpand(content: "a\nb\nc\nd", text: "a\nb\nc\nd"))
        XCTAssertFalse(BubblePreview.canExpand(content: "a\nb", text: "a\nb"))
        XCTAssertEqual(BubblePreview.promptLabel("read_thread"), "Read")
        XCTAssertEqual(BubblePreview.promptLabel("textmode"), "Textmode")
        XCTAssertEqual(BubblePreview.promptLabel("my_prompt"), "My prompt")
    }
}

/// Port of `utils/nodeLinks.test.js`.
@MainActor
final class NodeLinksTests: XCTestCase {
    private let origin = URL(string: "http://localhost:3001")!

    func testRecognisesLooreHostsTheCurrentOriginAndRelativePaths() {
        XCTAssertEqual(NodeLinks.nodeId("https://loore.org/node/24621"), 24621)
        XCTAssertEqual(NodeLinks.nodeId("https://www.loore.org/node/7/"), 7)
        XCTAssertEqual(NodeLinks.nodeId("https://staging.loore.org/node/3"), 3)
        XCTAssertEqual(NodeLinks.nodeId("http://localhost:3001/node/12", currentOrigin: origin), 12)
        XCTAssertEqual(NodeLinks.nodeId("/node/5"), 5)
    }

    func testRejectsOtherHostsOtherPathsAndJunk() {
        XCTAssertNil(NodeLinks.nodeId("https://example.com/node/5"))
        XCTAssertNil(NodeLinks.nodeId("https://loore.org/about"))
        XCTAssertNil(NodeLinks.nodeId("https://loore.org/node/abc"))
        XCTAssertNil(NodeLinks.nodeId("https://loore.org/node/5/edit"))
        XCTAssertNil(NodeLinks.nodeId("mailto:x@loore.org"))
        XCTAssertNil(NodeLinks.nodeId(""))
        XCTAssertNil(NodeLinks.nodeId(nil))
        XCTAssertNil(NodeLinks.nodeId("http://localhost:3002/node/12", currentOrigin: origin))
    }

    func testCoalescesSameTickLookupsIntoOneRequestAndCaches() async throws {
        let calls = CallLog()
        let store = NodeTitleStore(fetch: { ids in
            await calls.record(ids)
            let one = try JSONDecoder().decode(NodeTitlesResponse.Title.self, from: Data(#"{"id":1,"title":"One"}"#.utf8))
            return [1: one, 2: nil]
        })
        // Three main-actor tasks, queued before the flush the first one
        // schedules (as views asking in one render pass). `async let`
        // children hop to the main actor at their own pace, and on a slow
        // CI runner the flush ran between them.
        let a = Task { await store.lookup(1) }
        let b = Task { await store.lookup(2) }
        let c = Task { await store.lookup(1) }
        let (ra, rb, rc) = await (a.value, b.value, c.value)
        XCTAssertEqual(ra, .title("One"))
        XCTAssertEqual(rb, .inaccessible)
        XCTAssertEqual(rc, .title("One"))
        let requests = await calls.requests
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(Set(requests[0]), [1, 2])
        XCTAssertEqual(store.record(for: 1), .title("One"))
        _ = await store.lookup(1)
        let after = await calls.requests
        XCTAssertEqual(after.count, 1)
    }

    func testAFailedLookupResolvesNilAndIsNotCached() async {
        let calls = CallLog()
        let store = NodeTitleStore(fetch: { ids in
            let count = await calls.record(ids)
            if count == 1 { throw URLError(.notConnectedToInternet) }
            let nine = try JSONDecoder().decode(NodeTitlesResponse.Title.self, from: Data(#"{"id":9,"title":"Nine"}"#.utf8))
            return [9: nine]
        })
        let first = await store.lookup(9)
        XCTAssertNil(first)
        XCTAssertNil(store.records[9])
        let second = await store.lookup(9)
        XCTAssertEqual(second, .title("Nine"))
        let requests = await calls.requests
        XCTAssertEqual(requests.count, 2)
    }
}

actor CallLog {
    private(set) var requests: [[Int]] = []

    @discardableResult
    func record(_ ids: [Int]) -> Int {
        requests.append(ids)
        return requests.count
    }
}

final class ContentSegmenterTests: XCTestCase {
    func testSplitsOnQuoteAndArtifactMarkersLikeTheWeb() {
        XCTAssertEqual(ContentSegmenter.segments("a\n{quote:12}\nb {quote_ext:3}{user_recent_raw}{user_recent}c"), [
            .text("a\n"), .quote(12), .text("\nb "), .externalQuote(3), .artifact(.recentRaw), .artifact(.recent),
            .text("c"),
        ])
        XCTAssertEqual(ContentSegmenter.segments("{user_nope} stays"), [.text("{user_nope} stays")])
    }

    func testGuidanceMarkersAreReplacedOrRemoved() {
        XCTAssertEqual(ContentSegmenter.applyGuidance("A\n{share_guidance}\nB", shareGuidance: "G", externalGuidance: nil),
                       "A\nG\nB")
        XCTAssertEqual(ContentSegmenter.applyGuidance("A\n{external_content_guidance}\nB", shareGuidance: nil,
                                                      externalGuidance: nil), "A\nB")
        XCTAssertEqual(ContentSegmenter.applyGuidance("{share_guidance}", shareGuidance: "$1 \\n", externalGuidance: nil),
                       "$1 \\n\n")
    }

    func testPartialReplyTextDropsQuoteMarkersAndShareFences() {
        XCTAssertEqual(ContentSegmenter.partialReplyText("Hi {quote:4}there\n:::share insight\nbody\n:::\nend"),
                       "Hi there\nbody\nend")
        XCTAssertEqual(ContentSegmenter.quoteMarkerKey("x {quote:1} {quote_ext:2}"), "{quote:1},{quote_ext:2}")
    }
}
