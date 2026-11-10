import SwiftUI
import UIKit
import UIKit.UIGestureRecognizerSubclass

/// Text the user can select and copy, the way Safari and Notes allow: a long
/// press selects a word, the handles widen the selection, and the menu offers
/// Copy. SwiftUI's `.textSelection(.enabled)` on iOS copies a whole `Text` and
/// has no handles, so reading text is a non-editable `UITextView` sized to its
/// width (no scrolling of its own).
///
/// Links: a tap goes to the environment's `openURL` (inside `MarkdownView`, the
/// router's allowlist, #442); a long press on a link offers Open Link and Copy
/// Link instead of the system's preview, which would open it outside the router.
struct SelectableText: UIViewRepresentable {
    let text: NSAttributedString

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> ReadingTextView {
        let view = ReadingTextView(usingTextLayoutManager: true)
        view.isEditable = false
        view.isSelectable = true
        view.isScrollEnabled = false
        view.backgroundColor = .clear
        view.textContainerInset = .zero
        view.textContainer.lineFragmentPadding = 0
        view.adjustsFontForContentSizeCategory = false  // the fonts come scaled (`LooreUIFont.scaled`)
        view.dataDetectorTypes = []
        view.tintColor = LooreColor.accentUI
        view.linkTextAttributes = [.foregroundColor: LooreColor.accentUI,
                                   .underlineStyle: NSUnderlineStyle.single.rawValue]
        view.delegate = context.coordinator
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        view.setContentHuggingPriority(.defaultHigh, for: .vertical)
        view.addGestureRecognizer(context.coordinator.cardTap)
        return view
    }

    func updateUIView(_ view: ReadingTextView, context: Context) {
        context.coordinator.openURL = context.environment.openURL
        context.coordinator.origin = context.environment.markdownLinkOrigin
        context.coordinator.onTap = context.environment.readingTextTap?.action
        view.setReadingText(text)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView view: ReadingTextView, context: Context) -> CGSize? {
        view.setReadingText(text)
        let width = proposal.width.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
        return view.fittingSize(width: width)
    }

    @MainActor
    final class Coordinator: NSObject, UITextViewDelegate, UIGestureRecognizerDelegate {
        var openURL: OpenURLAction?
        var origin: URL?
        /// The tap of the card the text sits in (`readingTextTap`): the text view
        /// takes the touches, so a SwiftUI `onTapGesture` around it never sees them.
        var onTap: (() -> Void)?
        private(set) lazy var cardTap: QuickTapRecognizer = {
            let tap = QuickTapRecognizer(target: self, action: #selector(tapped(_:)))
            tap.cancelsTouchesInView = false
            tap.delegate = self
            return tap
        }()
        /// The text view held a selection when the tap began: that tap only clears it.
        private var hadSelection = false

        @objc private func tapped(_ tap: QuickTapRecognizer) {
            guard let view = tap.view as? ReadingTextView, let onTap, !hadSelection,
                  !view.hasLink(at: tap.location(in: view)) else { return }
            onTap()
        }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            guard onTap != nil, let view = gestureRecognizer.view as? UITextView else { return false }
            hadSelection = view.selectedRange.length > 0
            return true
        }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool {
            true
        }

        func textView(_ textView: UITextView, primaryActionFor textItem: UITextItem,
                      defaultAction: UIAction) -> UIAction? {
            guard case .link(let url) = textItem.content else { return defaultAction }
            return UIAction { [weak self] _ in self?.openURL?(url) }
        }

        func textView(_ textView: UITextView, menuConfigurationFor textItem: UITextItem,
                      defaultMenu: UIMenu) -> UITextItem.MenuConfiguration? {
            guard case .link(let url) = textItem.content else { return UITextItem.MenuConfiguration(menu: defaultMenu) }
            let absolute = URL(string: url.absoluteString, relativeTo: origin)?.absoluteURL ?? url
            let open = UIAction(title: "Open Link", image: UIImage(systemName: "arrow.up.right.square")) { [weak self] _ in
                self?.openURL?(url)
            }
            let copy = UIAction(title: "Copy Link", image: UIImage(systemName: "link")) { _ in
                UIPasteboard.general.url = absolute
            }
            // No preview: the system's would load the page outside the router.
            return UITextItem.MenuConfiguration(preview: nil, menu: UIMenu(children: [open, copy]))
        }

        /// A selection made in one text view clears the others' (one selection on
        /// screen, as in Safari), and a tap anywhere else clears it.
        func textViewDidChangeSelection(_ textView: UITextView) {
            guard textView.selectedRange.length > 0, let view = textView as? ReadingTextView else { return }
            if let current = ReadingTextView.selecting, current !== view {
                current.clearSelection()
            }
            ReadingTextView.selecting = view
            OutsideTapClearer.shared.watch(view)
        }
    }
}

/// A short tap: recognized when one finger lifts within `maximumDuration` without
/// moving, failed as soon as it is held longer. The text view's long press (which
/// selects) waits for a tap recognizer on the same view to fail; a
/// `UITapGestureRecognizer` fails only after about a second, so the long press
/// would need twice its usual hold. The limit sits just under the long press's
/// own delay (about 0.5 s), so a slow press on a card's text still opens the
/// card instead of doing nothing (#624 review: 0.3 s left 0.3–0.5 s dead).
final class QuickTapRecognizer: UIGestureRecognizer {
    static let maximumDuration: TimeInterval = 0.45
    static let allowableMovement: CGFloat = 10
    private var start: CGPoint = .zero
    private var touchID: ObjectIdentifier?

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        guard touchID == nil, touches.count == 1, let touch = touches.first else {
            state = .failed
            return
        }
        touchID = ObjectIdentifier(touch)
        start = touch.location(in: view)
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.maximumDuration) { [weak self, id = touchID] in
            guard let self, self.touchID == id, self.state == .possible else { return }
            self.state = .failed
        }
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent) {
        guard let touch = touches.first else { return }
        let point = touch.location(in: view)
        if hypot(point.x - start.x, point.y - start.y) > Self.allowableMovement { state = .failed }
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent) {
        state = state == .possible ? .ended : .failed
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent) {
        state = .failed
    }

    override func reset() {
        touchID = nil
    }
}

/// Clears the selection when the user taps outside the text view that holds it
/// (a `UITextView` keeps its selection until another view becomes first
/// responder, and most of a page never does). The tap itself still reaches
/// what it landed on.
@MainActor
final class OutsideTapClearer: NSObject, UIGestureRecognizerDelegate {
    static let shared = OutsideTapClearer()
    private var recognizer: UITapGestureRecognizer?

    func watch(_ view: ReadingTextView) {
        guard let window = view.window, recognizer?.view !== window else { return }
        if let old = recognizer { old.view?.removeGestureRecognizer(old) }
        let tap = UITapGestureRecognizer(target: self, action: #selector(tapped))
        tap.cancelsTouchesInView = false
        tap.delaysTouchesEnded = false
        tap.delegate = self
        window.addGestureRecognizer(tap)
        recognizer = tap
    }

    @objc private func tapped() {
        // After the tap's own action: a tap on the menu's Copy copies first.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
            ReadingTextView.selecting?.clearSelection()
        }
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        guard let selecting = ReadingTextView.selecting, selecting.window != nil,
              selecting.selectedRange.length > 0 else { return false }
        // The selection handles reach a little outside the text view.
        return !selecting.bounds.insetBy(dx: -20, dy: -20).contains(touch.location(in: selecting))
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                           shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool {
        true
    }
}

/// Plain (not markdown) reading text, selectable like `MarkdownView`'s: a
/// version of a document, text kept to copy from, an intention's lines.
struct ReadingText: View {
    let text: String
    /// Unscaled; scaled for Dynamic Type relative to `relativeTo`.
    var font: UIFont
    var relativeTo: Font.TextStyle = .body
    var color: UIColor
    var lineSpacing: CGFloat = 0
    var tracking: CGFloat = 0
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        SelectableText(text: attributed)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    var attributed: NSAttributedString {
        let traits = UITraitCollection(preferredContentSizeCategory: UIContentSizeCategory(typeSize))
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = lineSpacing
        paragraph.lineBreakStrategy = .standard
        return NSAttributedString(string: text, attributes: [
            .font: LooreUIFont.scaled(font, relativeTo: relativeTo, traits: traits),
            .foregroundColor: color,
            .paragraphStyle: paragraph,
            .kern: tracking,
        ])
    }
}

/// The text view behind `SelectableText`.
final class ReadingTextView: UITextView {
    /// The text view holding the visible selection.
    @MainActor static weak var selecting: ReadingTextView?

    private var fittingCache: (width: CGFloat, size: CGSize)?

    func setReadingText(_ text: NSAttributedString) {
        guard !attributedText.isEqual(to: text) else { return }
        attributedText = text
        fittingCache = nil
    }

    /// Height for `width`; with no width, the text's own width (one line per paragraph).
    func fittingSize(width: CGFloat?) -> CGSize {
        let key = width ?? -1
        if let cache = fittingCache, cache.width == key { return cache.size }
        let fitted = sizeThatFits(CGSize(width: width ?? .greatestFiniteMagnitude, height: .greatestFiniteMagnitude))
        let size = CGSize(width: width ?? ceil(fitted.width), height: ceil(fitted.height))
        fittingCache = (key, size)
        return size
    }

    /// Whether `point` is on a link's text.
    func hasLink(at point: CGPoint) -> Bool {
        guard let position = closestPosition(to: point) else { return false }
        let index = offset(from: beginningOfDocument, to: position)
        for i in [index - 1, index] where i >= 0 && i < attributedText.length {
            guard attributedText.attribute(.link, at: i, effectiveRange: nil) != nil,
                  let start = self.position(from: beginningOfDocument, offset: i),
                  let end = self.position(from: start, offset: 1),
                  let range = textRange(from: start, to: end) else { continue }
            if firstRect(for: range).insetBy(dx: -4, dy: -4).contains(point) { return true }
        }
        return false
    }

    func clearSelection() {
        guard selectedRange.length > 0 else { return }
        selectedRange = NSRange(location: 0, length: 0)
        if Self.selecting === self { Self.selecting = nil }
    }

    override func resignFirstResponder() -> Bool {
        let resigned = super.resignFirstResponder()
        if resigned { clearSelection() }
        return resigned
    }

    /// Plain text only: the attributed copy would carry the theme's text colour
    /// (light text on dark) into the app it is pasted in.
    override func copy(_ sender: Any?) {
        guard let range = Range(selectedRange, in: text), !range.isEmpty else { return }
        UIPasteboard.general.string = Self.plainText(String(text[range]))
    }

    /// What Copy puts on the pasteboard: line separators (a line break inside a
    /// paragraph) as newlines.
    static func plainText(_ selected: String) -> String {
        selected.replacingOccurrences(of: "\u{2028}", with: "\n")
    }
}

private struct MarkdownLinkOriginKey: EnvironmentKey {
    static let defaultValue: URL? = nil
}

private struct ReadingTextSelectableKey: EnvironmentKey {
    static let defaultValue = true
}

/// What a tap on reading text inside a tappable card does (the card's own tap).
struct ReadingTextTap {
    let action: () -> Void
}

private struct ReadingTextTapKey: EnvironmentKey {
    static let defaultValue: ReadingTextTap? = nil
}

extension View {
    /// The card's tap, also for taps on its selectable text (pair it with the
    /// card's `onTapGesture`). A tap on a link, or one that clears a selection,
    /// does not count.
    func readingTextTap(_ action: (() -> Void)?) -> some View {
        environment(\.readingTextTap, action.map(ReadingTextTap.init))
    }
}

extension EnvironmentValues {
    /// The Loore origin relative links resolve against (Copy Link).
    var markdownLinkOrigin: URL? {
        get { self[MarkdownLinkOriginKey.self] }
        set { self[MarkdownLinkOriginKey.self] = newValue }
    }

    /// Whether `MarkdownView` renders its text selectable (`SelectableText`)
    /// or as SwiftUI `Text` (as before selection existed).
    var readingTextSelectable: Bool {
        get { self[ReadingTextSelectableKey.self] }
        set { self[ReadingTextSelectableKey.self] = newValue }
    }

    var readingTextTap: ReadingTextTap? {
        get { self[ReadingTextTapKey.self] }
        set { self[ReadingTextTapKey.self] = newValue }
    }
}
