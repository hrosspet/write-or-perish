import XCTest
@testable import Loore

/// Port of `utils/markdownLinkedImages.test.js`.
final class MarkdownLinkedImagesTests: XCTestCase {
    private let img = "![A pic](https://cdn.example.com/image/fetch/w_1456/https%3A%2F%2Fmedia.example%2Fpic.jpeg)"
    private let href = "https://cdn.example.com/image/fetch/https%3A%2F%2Fmedia.example%2Fpic.jpeg"
    private var broken: String { "[\n\n\(img)\n\n](\(href))" }
    private var fixed: String { "[\(img)](\(href))" }

    private func normalize(_ s: String) -> String { MarkdownLinkedImages.normalize(s) }

    func testBrokenShapeBecomesALinkWithAnImageInside() {
        XCTAssertEqual(normalize("Intro\n\n\(broken)\n\nOutro"), "Intro\n\n\(fixed)\n\nOutro")
        XCTAssertEqual(normalize("\(broken)\n\ntext\n\n\(broken)"), "\(fixed)\n\ntext\n\n\(fixed)")
    }

    func testRepairedTextParsesAsALinkAroundAnImage() {
        let doc = MarkdownParser.parse("Intro\n\n\(broken)\n")
        guard doc.blocks.count == 2, case .paragraph(let inlines) = doc.blocks[1].kind,
              inlines.count == 1, case .link(let destination, let children) = inlines[0] else {
            return XCTFail("expected one paragraph holding one link, got \(doc.blocks)")
        }
        XCTAssertEqual(destination, href)
        XCTAssertTrue(children.containsImage)
    }

    func testCodeIsUnchanged() {
        for text in ["Before\n\n```\n\(broken)\n```\n\nAfter", "~~~md\n\(broken)\n~~~", "```\n\(broken)",
                     "Use `\(broken.replacingOccurrences(of: "\n", with: " "))` like so"] {
            XCTAssertEqual(normalize(text), text)
        }
        XCTAssertEqual(normalize("```\ncode\n```\n\n\(broken)"), "```\ncode\n```\n\n\(fixed)")
    }

    func testNormalMarkdownIsUnchanged() {
        for text in ["See [this](https://a.example/x) and [that](https://b.example).", img, fixed,
                     "[ \(img) text ](\(href))", "[\n\n\(img)\n\n\(img)\n\n](\(href))",
                     "- [ ] a task\n- [x] done", "[ ]\n\n![a](b)\n\n(c)"] {
            XCTAssertEqual(normalize(text), text)
        }
    }
}
