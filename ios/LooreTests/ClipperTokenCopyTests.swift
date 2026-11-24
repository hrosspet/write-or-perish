import UIKit
import UniformTypeIdentifiers
import XCTest
@testable import Loore

/// Copying a new clipper token: what is put on the clipboard, when the copy
/// ends, and that it is still offered to the person's other Apple devices.
final class ClipperTokenCopyTests: XCTestCase {
    private final class RecordingPasteboard: PasteboardWriting {
        var items: [[String: Any]] = []
        var options: [UIPasteboard.OptionsKey: Any] = [:]
        var calls = 0

        func setItems(_ items: [[String: Any]], options: [UIPasteboard.OptionsKey: Any]) {
            self.items = items
            self.options = options
            calls += 1
        }
    }

    func testTheTokenIsCopiedAsOnePlainTextItem() {
        let pasteboard = RecordingPasteboard()
        ClipperTokenCopy.copy("loore_abc123", to: pasteboard)
        XCTAssertEqual(pasteboard.calls, 1)
        XCTAssertEqual(pasteboard.items.count, 1)
        XCTAssertEqual(pasteboard.items.first?.count, 1)
        XCTAssertEqual(pasteboard.items.first?[UTType.utf8PlainText.identifier] as? String, "loore_abc123")
    }

    func testTheCopyEndsFiveMinutesAfterTheTap() {
        let pasteboard = RecordingPasteboard()
        let tap = Date(timeIntervalSince1970: 1_800_000_000)
        ClipperTokenCopy.copy("loore_abc123", to: pasteboard, now: tap)
        XCTAssertEqual(ClipperTokenCopy.minutesOnClipboard, 5)
        XCTAssertEqual(pasteboard.options[.expirationDate] as? Date, tap.addingTimeInterval(300))
    }

    func testTheCopyIsNotKeptToThisDevice() {
        let pasteboard = RecordingPasteboard()
        ClipperTokenCopy.copy("loore_abc123", to: pasteboard)
        XCTAssertNil(pasteboard.options[.localOnly])
        XCTAssertEqual(pasteboard.options.count, 1)
    }

    func testTheLineAfterTheTapSaysHowLongTheCopyLasts() {
        XCTAssertEqual(ClipperTokenCopy.copiedText, "Copied. It stays on the clipboard for 5 minutes.")
    }

    func testTheSystemClipboardHoldsTheTokenAfterTheCopy() {
        UIPasteboard.general.string = ""
        ClipperTokenCopy.copy("loore_abc123")
        XCTAssertEqual(UIPasteboard.general.string, "loore_abc123")
        UIPasteboard.general.string = ""
    }
}
