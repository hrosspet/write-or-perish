import Foundation

/// Optimistic edits of one text (a checklist tick, the row "+", a proposal row
/// moved), saved only over the text the screen shows (#795; the web's
/// `useChecklistEdits`). Peter, 2026-11-24: the tick shows at once, and it is
/// saved only if it skips no newer version.
///
/// Each save sends the new text and its base, the text the edit was made on
/// (`base_content`). Saves go one after the other: a quick second tick waits
/// for the first and is sent on the text the first one saved. When the server
/// answers 409 `content_changed` (the text changed elsewhere: another device,
/// a tab, the AI), every edit still waiting is undone and a toast says there
/// is a newer version; the screen keeps its text. Any other failure undoes
/// that edit and names the reason.
@MainActor
final class ContentEditQueue {
    static let contentChangedCode = "content_changed"

    /// Where the edits of one call go. Read when they are needed, so a screen
    /// that changed meanwhile is not undone over.
    struct Target {
        /// The text on screen now; nil when there is none to edit.
        var onScreen: () -> String?
        /// Puts a text on screen.
        var show: (String) -> Void
        /// Sends the new text with its base; throws on failure.
        var save: (_ content: String, _ base: String) async throws -> Void
        /// Shows a toast.
        var notify: (String) -> Void
        /// The toast for a refusal because the text changed elsewhere.
        var newerMessage: String
    }

    private struct Entry {
        let id = UUID()
        let apply: (String) -> String
        let failure: (String) -> String
        let target: Target
    }

    /// The text the server has, as far as this screen knows.
    private var base = ""
    /// Edits shown but not saved yet, oldest first.
    private var pending: [Entry] = []
    /// The text this queue last put on screen.
    private var shown: String?
    /// The last save; the next one waits for it.
    private var chain: Task<Void, Never>?

    /// Shows `edit` applied to the screen's text and queues its save.
    /// `failure` makes the toast of a failure other than the refusal from the
    /// reason ("Couldn't save change — reverted (…)"). No change, no save.
    func apply(_ edit: @escaping (String) -> String, to target: Target,
               failure: @escaping (String) -> String) {
        guard let current = target.onScreen() else { return }
        let next = edit(current)
        guard next != current else { return }
        // Nothing of this text is being saved: the screen shows what the
        // server sent, the base of this edit.
        if pending.isEmpty { base = current }
        let entry = Entry(apply: edit, failure: failure, target: target)
        pending.append(entry)
        shown = next
        target.show(next)
        let previous = chain
        chain = Task { [weak self] in
            await previous?.value
            await self?.send(entry)
        }
    }

    /// Resolves once every queued save has finished (tests).
    func settled() async {
        await chain?.value
    }

    private func send(_ entry: Entry) async {
        // Dropped: an edit before it was refused.
        guard pending.contains(where: { $0.id == entry.id }) else { return }
        let base = self.base
        let content = entry.apply(base)
        guard content != base else {
            drop(entry)
            return
        }
        do {
            try await entry.target.save(content, base)
            self.base = content
            drop(entry)
        } catch {
            if Self.isContentChanged(error) {
                pending.removeAll()
                undo(to: base, entry.target)
                entry.target.notify(entry.target.newerMessage)
                return
            }
            drop(entry)
            undo(to: pending.reduce(base) { text, edit in edit.apply(text) }, entry.target)
            let apiError = error as? APIError
            let reason = apiError?.serverMessage ?? apiError?.errorDescription ?? error.localizedDescription
            entry.target.notify(entry.failure(reason))
        }
    }

    private func drop(_ entry: Entry) {
        pending.removeAll { $0.id == entry.id }
    }

    /// Back to `text` on screen, unless something else (a reload) put another
    /// text there meanwhile.
    private func undo(to text: String, _ target: Target) {
        guard target.onScreen() == shown else { return }
        shown = text
        target.show(text)
    }

    /// The server's "the text changed since the screen showed it".
    static func isContentChanged(_ error: Error) -> Bool {
        guard let apiError = error as? APIError else { return false }
        return apiError.status == 409 && apiError.code == contentChangedCode
    }
}
