import SwiftUI
import UIKit
import XCTest
@testable import Loore

/// Selectable reading text (Peter, 2026-11-10: "currently it's not possible to
/// select text in the app and copy it"): which markdown blocks share one text
/// view, what that text holds, what Copy puts on the pasteboard, and that the
/// rendered text is selectable and not editable. The gestures themselves are in
/// `SelectCopyUITests` (local backend).
@MainActor
final class SelectableTextTests: XCTestCase {
    private func context(_ style: MarkdownStyle = .focal) -> MarkdownRenderContext {
        MarkdownRenderContext(style: style, checklist: nil, edit: ChecklistEditState(), titles: NodeTitleStore(),
                              environment: .local)
    }

    private let traits = UITraitCollection(preferredContentSizeCategory: .large)

    private func build(_ markdown: String, style: MarkdownStyle = .focal) -> NSAttributedString {
        MarkdownFlowBuilder.build(MarkdownParser.parse(markdown).blocks, context: context(style), traits: traits)
    }

    private func kinds(_ markdown: String) -> [Bool] {
        MarkdownFlow.segments(MarkdownParser.parse(markdown).blocks).map(\.isFlow)
    }

    // MARK: Which blocks share a text

    func testParagraphsHeadingsAndListsShareOneText() {
        let segments = MarkdownFlow.segments(MarkdownParser.parse("# Title\n\nOne.\n\n- a\n- b\n  1. c\n\nTwo.").blocks)
        XCTAssertEqual(segments.map(\.isFlow), [true])
        XCTAssertEqual(segments.first?.range, 0..<4)
    }

    func testCodeQuotesTablesRulesTaskListsAndImagesKeepTheirOwnViews() {
        XCTAssertEqual(kinds("One.\n\n```\ncode\n```\n\nTwo."), [true, false, true])
        XCTAssertEqual(kinds("One.\n\n> quoted\n\nTwo."), [true, false, true])
        XCTAssertEqual(kinds("One.\n\n| a | b |\n|---|---|\n| 1 | 2 |"), [true, false])
        XCTAssertEqual(kinds("One.\n\n---\n\nTwo."), [true, false, true])
        XCTAssertEqual(kinds("- [ ] task\n- [x] done"), [false])
        XCTAssertEqual(kinds("Text ![alt](https://loore.org/a.png) more"), [false])
        // A list with a code block in an item keeps the list's own view.
        XCTAssertEqual(kinds("- item\n\n  ```\n  code\n  ```"), [false])
    }

    func testARunKeepsItsFirstBlocksIdSoAStreamingReplyKeepsItsView() {
        let short = MarkdownFlow.segments(MarkdownParser.parse("One.").blocks)
        let longer = MarkdownFlow.segments(MarkdownParser.parse("One.\n\nTwo.\n\nThree.").blocks)
        XCTAssertEqual(short.first?.id, longer.first?.id)
    }

    // MARK: What the text holds

    func testTheTextReadsAsThePageDoes() {
        let text = build("## Heading\n\nFirst **bold** line.\n\n- one\n- two\n  - nested\n\n3. three\n4. four").string
        XCTAssertEqual(text, "Heading\nFirst bold line.\n•\tone\n•\ttwo\n◦\tnested\n3.\tthree\n4.\tfour")
    }

    func testALineBreakInsideAParagraphStaysInTheParagraphAndCopiesAsANewline() {
        let text = build("first line\nsecond line").string
        XCTAssertEqual(text, "first line\u{2028}second line")
        XCTAssertEqual(ReadingTextView.plainText(text), "first line\nsecond line")
    }

    func testLinksStayLinksAndHeadingsKeepTheirLevelForVoiceOver() {
        let text = build("# Title\n\nSee [the site](https://example.com).")
        let range = (text.string as NSString).range(of: "the site")
        XCTAssertEqual(text.attribute(.link, at: range.location, effectiveRange: nil) as? URL,
                       URL(string: "https://example.com"))
        XCTAssertEqual(text.attribute(.accessibilityTextHeadingLevel, at: 0, effectiveRange: nil) as? Int, 1)
        XCTAssertNil(text.attribute(.accessibilityTextHeadingLevel, at: range.location, effectiveRange: nil))
    }

    func testParagraphGapsMatchTheBlocksWithoutTextKitsExtraLineSpacing() throws {
        let style = MarkdownStyle.bubble
        let text = build("One.\n\nTwo.", style: style)
        let second = (text.string as NSString).range(of: "Two.").location
        let paragraph = try XCTUnwrap(text.attribute(.paragraphStyle, at: second, effectiveRange: nil) as? NSParagraphStyle)
        XCTAssertEqual(paragraph.lineSpacing, style.lineSpacing, accuracy: 0.01)
        // TextKit puts the first paragraph's line spacing under it; the gap makes up the rest.
        XCTAssertEqual(paragraph.paragraphSpacingBefore + style.lineSpacing, style.paragraphSpacing, accuracy: 0.01)
        let first = try XCTUnwrap(text.attribute(.paragraphStyle, at: 0, effectiveRange: nil) as? NSParagraphStyle)
        XCTAssertEqual(first.paragraphSpacingBefore, 0)
    }

    func testListItemsIndentTheirTextPastTheMarker() throws {
        let text = build("- one\n  - nested")
        let nested = (text.string as NSString).range(of: "nested").location
        let outer = try XCTUnwrap(text.attribute(.paragraphStyle, at: 0, effectiveRange: nil) as? NSParagraphStyle)
        let inner = try XCTUnwrap(text.attribute(.paragraphStyle, at: nested, effectiveRange: nil) as? NSParagraphStyle)
        let step = MarkdownSpacing.markerColumn + MarkdownSpacing.markerGap
        XCTAssertEqual(outer.headIndent, step)
        XCTAssertEqual(inner.headIndent, 2 * step)
        XCTAssertEqual(outer.tabStops.first?.location, step)
    }

    func testADoneTodoItemIsStruckThrough() {
        let text = build("Water the plants", style: MarkdownStyle.todoItem.with { $0.strikethrough = true })
        XCTAssertEqual(text.attribute(.strikethroughStyle, at: 0, effectiveRange: nil) as? Int,
                       NSUnderlineStyle.single.rawValue)
        XCTAssertNil(build("Water the plants", style: .todoItem).attribute(.strikethroughStyle, at: 0, effectiveRange: nil))
    }

    func testColoursFollowTheTheme() throws {
        let text = build("Plain words.", style: .bubble)
        let colour = try XCTUnwrap(text.attribute(.foregroundColor, at: 0, effectiveRange: nil) as? UIColor)
        let dark = colour.resolvedColor(with: UITraitCollection(userInterfaceStyle: .dark))
        let light = colour.resolvedColor(with: UITraitCollection(userInterfaceStyle: .light))
        XCTAssertNotEqual(dark, light, "the text colour does not change with the theme")
    }

    func testFontsGrowWithDynamicType() throws {
        let large = build("Words.")
        let huge = MarkdownFlowBuilder.build(MarkdownParser.parse("Words.").blocks, context: context(),
                                             traits: UITraitCollection(preferredContentSizeCategory: .accessibilityExtraLarge))
        let small = try XCTUnwrap(large.attribute(.font, at: 0, effectiveRange: nil) as? UIFont)
        let big = try XCTUnwrap(huge.attribute(.font, at: 0, effectiveRange: nil) as? UIFont)
        XCTAssertEqual(small.pointSize, MarkdownStyle.focal.fontSize, accuracy: 0.01)
        XCTAssertGreaterThan(big.pointSize, small.pointSize)
    }

    // MARK: The text view

    private func textView(_ text: NSAttributedString) -> ReadingTextView {
        let view = ReadingTextView(usingTextLayoutManager: true)
        view.isEditable = false
        view.isScrollEnabled = false
        view.textContainerInset = .zero
        view.textContainer.lineFragmentPadding = 0
        view.setReadingText(text)
        return view
    }

    func testCopyPutsOnlyTheSelectedPartAsPlainText() {
        let view = textView(build("The first paragraph.\n\nA second\nline."))
        let start = (view.text as NSString).range(of: "first").location
        let end = (view.text as NSString).range(of: "second").location + "second".count
        view.selectedRange = NSRange(location: start, length: end - start)
        UIPasteboard.general.string = ""
        view.copy(nil)
        XCTAssertEqual(UIPasteboard.general.string, "first paragraph.\nA second")
        XCTAssertFalse(UIPasteboard.general.hasURLs)
    }

    func testATapFindsALinkOnlyOnTheLinksText() {
        let view = textView(build("[Link](https://example.com) and then plain words that run on."))
        view.frame = CGRect(x: 0, y: 0, width: 300, height: view.fittingSize(width: 300).height)
        view.layoutIfNeeded()
        XCTAssertTrue(view.hasLink(at: CGPoint(x: 6, y: 8)))
        XCTAssertFalse(view.hasLink(at: CGPoint(x: 200, y: 8)))
    }

    func testTheHeightIsTheTextsHeightAtTheGivenWidth() {
        let view = textView(build("One line.\n\nAnother line."))
        let size = view.fittingSize(width: 300)
        let font = MarkdownStyle.focal.uiFont(traits: traits)
        XCTAssertEqual(size.width, 300)
        XCTAssertEqual(size.height, ceil(2 * font.lineHeight + MarkdownStyle.focal.paragraphSpacing), accuracy: 2)
    }

    func testALongReplyLaysOutQuickly() {
        let paragraph = String(repeating: "A sentence about the river and the hills. ", count: 12)
        let markdown = (0..<150).map { $0 % 10 == 0 ? "## Part \($0)" : paragraph }.joined(separator: "\n\n")
        let started = Date()
        let view = textView(build(markdown))
        let height = view.fittingSize(width: 330).height
        XCTAssertGreaterThan(height, 1000)
        XCTAssertLessThan(Date().timeIntervalSince(started), 1.0, "a 13,000-word reply took over a second")
    }

    // MARK: In the page

    private func host<V: View>(_ view: V) throws -> (UIWindow, UIView) {
        let app = AppState(launch: LaunchOptions(), secureStore: InMemorySecureStore(),
                           defaults: UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")!)
        let controller = UIHostingController(rootView: view.environment(app))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 390, height: 844)
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.view.layoutIfNeeded()
        return (window, controller.view)
    }

    private func textViews(in view: UIView) -> [UITextView] {
        ((view as? UITextView).map { [$0] } ?? []) + view.subviews.flatMap { textViews(in: $0) }
    }

    func testAMarkdownBodyIsSelectableNotEditableText() async throws {
        let (window, root) = try host(MarkdownView(markdown: "# Title\n\nOne.\n\n- a\n- b\n\n> quoted\n\nTwo."))
        defer { window.isHidden = true }
        try await Task.sleep(nanoseconds: 300_000_000)
        let views = textViews(in: root)
        XCTAssertEqual(views.map(\.text), ["Title\nOne.\n•\ta\n•\tb", "quoted", "Two."])
        for view in views {
            XCTAssertTrue(view.isSelectable)
            XCTAssertFalse(view.isEditable)
            XCTAssertFalse(view.isScrollEnabled)
        }
    }

    func testTurningSelectionOffRendersTheBlocksAsBefore() async throws {
        let (window, root) = try host(MarkdownView(markdown: "One.\n\nTwo.").environment(\.readingTextSelectable, false))
        defer { window.isHidden = true }
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertTrue(textViews(in: root).isEmpty)
    }
}
