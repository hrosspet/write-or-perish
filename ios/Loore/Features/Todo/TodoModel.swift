import Foundation
import Observation

/// The todo document and its saves (web `TodoPage` + `useCheckboxToggle` /
/// `useTaskInsert`). Saves overwrite the latest version without a version
/// check (map E §15), so every in-place edit re-fetches the todo right before
/// its `PATCH` and applies the same text-keyed edit to the fresh content
/// (design doc §10); the page also re-fetches after "todo changed".
@MainActor
@Observable
final class TodoModel {
    private(set) var todo: TodoDoc?
    private(set) var loading = true
    weak var app: AppState?

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
        todo = envelope.todo
        return todo
    }

    /// `PUT /api/todo/ {content, generated_by:'user'}`: a new version (edit-mode
    /// Save and the first create). Returns false on failure.
    func save(_ content: String) async -> Bool {
        guard let app, !content.jsTrimmed.isEmpty else { return false }
        do {
            let envelope: TodoEnvelope = try await app.api.put(
                APIPath.todo, json: ["content": .string(content), "generated_by": "user"])
            todo = envelope.todo
            app.signals.post(.todoChanged)
            return true
        } catch {
            return false
        }
    }

    func adopt(_ todo: TodoDoc?) {
        self.todo = todo
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

    /// Optimistic in-place edit: apply `edit` to what is shown, re-fetch, apply
    /// the same edit to the server's latest content, `PATCH` it. On failure the
    /// shown content goes back and (with `failure`) a toast names the reason.
    func patch(failure: String?, skipUnchanged: Bool = false, edit: (String) -> String) async -> Bool {
        guard let app, let current = todo else { return false }
        let optimistic = edit(current.content)
        if skipUnchanged && optimistic == current.content { return false }
        todo?.content = optimistic
        do {
            let fresh: TodoEnvelope = try await app.api.get(APIPath.todo, poll: true)
            let updated = edit(fresh.todo?.content ?? current.content)
            let answer: TodoEnvelope = try await app.api.patch(APIPath.todo, json: ["content": .string(updated)])
            if let saved = answer.todo { todo = saved } else { todo?.content = updated }
            return true
        } catch {
            todo?.content = current.content
            if let failure {
                let apiError = error as? APIError
                let reason = apiError?.serverMessage ?? apiError?.errorDescription ?? "Unknown error"
                app.toasts.show("\(failure) (\(reason))")
            }
            return false
        }
    }
}
