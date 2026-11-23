import UIKit
import UniformTypeIdentifiers

/// The part of `UIPasteboard` the token copy uses, so a test can read what
/// was passed.
protocol PasteboardWriting {
    func setItems(_ items: [[String: Any]], options: [UIPasteboard.OptionsKey: Any])
}

extension UIPasteboard: PasteboardWriting {}

/// Copying a new clipper token: the copy leaves the clipboard by itself after
/// a few minutes, and it is still handed to the person's other Apple devices,
/// because pasting it into Chrome on the Mac is what it is copied for.
enum ClipperTokenCopy {
    /// How long the copied token stays on the clipboard. Five minutes: the
    /// token is pasted into the extension's options right after it is made,
    /// on the Mac that is usually already open (Peter's decision, 2026-11-23:
    /// "a few minutes"). If the time passes first, the dialog is still open
    /// and one more tap copies it again.
    static let minutesOnClipboard = 5

    /// Shown under the token after the tap, and spoken by VoiceOver.
    static let copiedText = "Copied. It stays on the clipboard for \(minutesOnClipboard) minutes."

    /// No `.localOnly` option: Universal Clipboard carries the copy to the Mac.
    static func copy(_ token: String, to pasteboard: PasteboardWriting = UIPasteboard.general, now: Date = Date()) {
        pasteboard.setItems(
            [[UTType.utf8PlainText.identifier: token]],
            options: [.expirationDate: now.addingTimeInterval(TimeInterval(minutesOnClipboard * 60))]
        )
    }
}
