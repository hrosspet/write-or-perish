import Foundation
import Observation

/// The todo document and its saves (web `TodoPage` + `useCheckboxToggle` /
/// `useTaskInsert`). Every in-place edit re-fetches the todo right before its
/// `PATCH` and applies the same text-keyed edit to the fresh content (design
/// doc §10); the page also re-fetches after "todo changed". The `PATCH` sends
/// the revision of the list it edited (#477): when a save, revert or todo merge
/// landed in between, the server refuses with the newest list (409
/// `todo_changed`), and the edit is applied to that list and saved once more,
/// as on the web (#430). The editor's Save sends the revision the editor was
/// opened on (#476); a refusal comes back as `.changed` for the page to offer
/// the choice.
///
/// In-place edits are saved one at a time, in tap order: an edit's re-fetch
/// waits for the previous edit's `PATCH`, so two quick ticks (or a quick-add and
/// a tick) both reach the server. What is shown is the last todo the server
/// returned plus the edits still being saved.
@MainActor
@Observable
final class TodoModel {
    private(set) var todo: TodoDoc?
    private(set) var loading = true
    weak var app: AppState?

    /// The last todo the server returned.
    @ObservationIgnored private var confirmed: TodoDoc?
    /// Edits shown optimistically whose `PATCH` has not answered yet, oldest first.
    @ObservationIgnored private var pending: [PendingEdit] = []
    /// The network step of the last edit; the next edit's step waits for it.
    @ObservationIgnored private var saveQueue: Task<Void, Never>?

    private struct PendingEdit {
        let id = UUID()
        let apply: (String) -> String
    }

    init(app: AppState? = nil) {
        self.app = app
    }

    /// `GET /api/todo/`. Errors are logged only on the web; the list stays.
    @discardableResult
    func fetch() async -> TodoDoc? {
        defer { loading = false }
        guard let api = app?.api, let envelope: TodoEnvelope = try? await api.get(APIPath.todo, poll: true) else {
            return todo
        }
        showServer(envelope.todo)
        return todo
    }

    /// What an editor Save came to.
    enum SaveResult: Equatable {
        case saved
        /// The list changed after the editor was opened (#476): nothing was
        /// saved. The newest version, which the page now shows.
        case changed(TodoDoc)
        /// Any other failure; a toast says why.
        case failed
    }

    /// `PUT /api/todo/ {content, generated_by:'user', base_revision}`: a new
    /// version (edit-mode Save and the first create). `base` is the version the
    /// editor was opened on, or the newest one for "Save mine anyway"; nil (the
    /// first create) saves without the check.
    func save(_ content: String, over base: TodoDoc?) async -> SaveResult {
        guard let app, !content.jsTrimmed.isEmpty else { return .failed }
        var body: [String: JSONValue] = ["content": .string(content), "generated_by": "user"]
        if let revision = base?.revision { body["base_revision"] = .string(revision) }
        do {
            let envelope: TodoEnvelope = try await app.api.put(APIPath.todo, json: .object(body))
            showServer(envelope.todo)
            app.signals.post(.todoChanged)
            return .saved
        } catch {
            if let newest = Self.newestTodo(in: error) {
                showServer(newest)
                return .changed(newest)
            }
            let apiError = error as? APIError
            let reason = apiError?.serverMessage ?? apiError?.errorDescription ?? "Unknown error"
            app.toasts.show("Couldn't save the todo list (\(reason))")
            return .failed
        }
    }

    /// The newest version a 409 `todo_changed` answer carries, or nil.
    static func newestTodo(in error: Error) -> TodoDoc? {
        guard let apiError = error as? APIError, apiError.status == 409,
              apiError.code == "todo_changed",
              let json = apiError.body?.extra["todo"],
              let data = try? JSONEncoder().encode(json) else { return nil }
        return try? APIClient.makeDecoder().decode(TodoDoc.self, from: data)
    }

    func adopt(_ todo: TodoDoc?) {
        showServer(todo)
    }

    /// Tick / untick by the item's stripped label (every matching line).
    func toggle(_ item: TodoSections.Item) async {
        let key = MarkdownEdits.stripInlineMarkdown(item.text).jsTrimmed
        let checked = item.checked ?? false
        _ = await patch(failure: "Couldn't save change — reverted") { content in
            MarkdownEdits.toggleCheckbox(content, itemText: key, currentChecked: checked)
        }
    }

    /// The row "+": a sibling after the item and its subtree.
    func insertAfter(_ item: TodoSections.Item, text: String) async {
        let key = MarkdownEdits.stripInlineMarkdown(item.text).jsTrimmed
        _ = await patch(failure: "Couldn't add task — reverted", skipUnchanged: true) { content in
            MarkdownEdits.insertItemAfter(content, afterItemText: key, newText: text)
        }
    }

    /// Quick-add to Today (created at the top when missing). False on failure
    /// (the page then puts the typed text back; no toast, as on the web).
    func quickAdd(_ task: String) async -> Bool {
        let clean = task.jsTrimmed
        guard !clean.isEmpty, todo != nil else { return false }
        return await patch(failure: nil) { content in
            MarkdownEdits.appendItemToSection(content, sectionTitle: "Today", task: clean, createAtStart: true)
        }
    }

    /// Optimistic in-place edit: apply `edit` to what is shown, then (after the
    /// edits before it) re-fetch, apply the same edit to the server's latest
    /// content and `PATCH` it. On failure the shown content goes back to the
    /// server's latest plus the edits still pending, and (with `failure`) a toast
    /// names the reason.
    func patch(failure: String?, skipUnchanged: Bool = false, edit: @escaping (String) -> String) async -> Bool {
        guard app != nil, let current = todo else { return false }
        let optimistic = edit(current.content)
        if skipUnchanged && optimistic == current.content { return false }
        let entry = PendingEdit(apply: edit)
        pending.append(entry)
        todo?.content = optimistic
        let previous = saveQueue
        let step = Task { [weak self] () -> Bool in
            await previous?.value
            guard let self else { return false }
            return await self.send(entry, fallback: current.content, failure: failure)
        }
        saveQueue = Task { _ = await step.value }
        return await step.value
    }

    private func send(_ entry: PendingEdit, fallback: String, failure: String?) async -> Bool {
        guard let app else {
            finish(entry, server: confirmed)
            return false
        }
        do {
            let fresh: TodoEnvelope = try await app.api.get(APIPath.todo, poll: true)
            if let latest = fresh.todo { confirmed = latest }
            var base = fresh.todo
            var retried = false
            while true {
                do {
                    let saved = try await sendPatch(entry, on: base, fallback: fallback, app: app)
                    finish(entry, server: saved)
                    return true
                } catch {
                    // A save, revert or todo merge landed between the GET and the
                    // PATCH: the edit is applied to the newest list the 409 carries
                    // and saved once more. A second 409 in a row is reported.
                    guard !retried, let newest = Self.newestTodo(in: error) else { throw error }
                    retried = true
                    confirmed = newest
                    base = newest
                }
            }
        } catch {
            if let newest = Self.newestTodo(in: error) { confirmed = newest }
            finish(entry, server: confirmed)
            if let failure {
                let apiError = error as? APIError
                let reason = apiError?.serverMessage ?? apiError?.errorDescription ?? "Unknown error"
                app.toasts.show("\(failure) (\(reason))")
            }
            return false
        }
    }

    /// `PATCH` the edit applied to `base` (the newest list the app has), with
    /// `base`'s revision. Returns the saved version.
    private func sendPatch(_ entry: PendingEdit, on base: TodoDoc?, fallback: String, app: AppState) async throws -> TodoDoc? {
        let updated = entry.apply(base?.content ?? fallback)
        var body: [String: JSONValue] = ["content": .string(updated)]
        if let revision = base?.revision { body["base_revision"] = .string(revision) }
        let answer: TodoEnvelope = try await app.api.patch(APIPath.todo, json: .object(body))
        var saved = answer.todo ?? confirmed
        if answer.todo == nil { saved?.content = updated }
        return saved
    }

    private func finish(_ entry: PendingEdit, server: TodoDoc?) {
        pending.removeAll { $0.id == entry.id }
        showServer(server)
    }

    /// Shows `server` (the server's latest) with the edits still being saved applied on top.
    private func showServer(_ server: TodoDoc?) {
        confirmed = server
        guard var shown = server else {
            todo = nil
            return
        }
        shown.content = pending.reduce(shown.content) { content, edit in edit.apply(content) }
        todo = shown
    }
}
