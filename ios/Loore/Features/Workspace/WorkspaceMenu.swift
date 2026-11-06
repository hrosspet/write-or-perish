import Foundation
import Observation

/// One bubble of the ArtifactsNav row.
struct WorkspaceBubble: Identifiable, Equatable {
    let document: WorkspaceDocument
    let title: String
    /// The listed artifact behind an artifact bubble (nil for Profile, Todo and "+").
    var artifact: ArtifactDoc?

    var id: WorkspaceDocument { document }
}

/// What a long press on a bubble offers (#603).
enum WorkspaceMenuItem: Equatable {
    case edit
    case delete
}

/// The row's order, each bubble's menu, and the selection after a delete (#603).
enum WorkspaceMenu {
    /// The row (web `ArtifactsNav`): Profile, Intentions, Todo, the other
    /// built-in kinds in `builtinKindOrder`, the rest by title, then "+".
    static func bubbles(_ artifacts: [ArtifactDoc]) -> [WorkspaceBubble] {
        let byKind = Dictionary(artifacts.map { ($0.kind, $0) }, uniquingKeysWith: { first, _ in first })
        let pinned = ArtifactKinds.builtinKindOrder.filter { $0 != "intentions" }.compactMap { byKind[$0] }
        var seen = Set<String>()
        let custom = artifacts.filter { !ArtifactKinds.isBuiltinKind($0.kind) && seen.insert($0.kind).inserted }
            .sorted { ArtifactKinds.titleSortsBefore(label($0), label($1)) }
        var row = [WorkspaceBubble(document: .profile, title: "Profile")]
        if let intentions = byKind["intentions"] { row.append(bubble(intentions)) }
        row.append(WorkspaceBubble(document: .todo, title: "Todo"))
        row += pinned.map(bubble)
        row += custom.map(bubble)
        row.append(WorkspaceBubble(document: .create, title: "+"))
        return row
    }

    /// Custom artifacts get Edit and Delete. Built-in kinds, the Saved References
    /// Digest, Profile and Todo get Edit only (Peter, 2026-11-06: Profile and Todo
    /// behave like Predictions). "+" gets no menu. Delete follows the list's
    /// `deletable`, so the rule stays on the server.
    static func items(for bubble: WorkspaceBubble) -> [WorkspaceMenuItem] {
        switch bubble.document {
        case .create: return []
        case .profile, .todo: return [.edit]
        case .artifact: return bubble.artifact?.deletable == true ? [.edit, .delete] : [.edit]
        }
    }

    /// The document to open after `kind` was deleted: the next bubble, or the
    /// previous one when it was the last ("+" is not counted). nil when the open
    /// document was another one, which stays open.
    static func selection(afterDeleting kind: String, active: WorkspaceDocument,
                          row: [WorkspaceBubble]) -> WorkspaceDocument? {
        let deleted = WorkspaceDocument.artifact(kind)
        guard active == deleted else { return nil }
        let documents = row.map(\.document).filter { $0 != .create }
        guard let index = documents.firstIndex(of: deleted) else { return nil }
        if index + 1 < documents.count { return documents[index + 1] }
        return index > 0 ? documents[index - 1] : nil
    }

    /// A bubble's text: the title, or the kind when the title is empty.
    static func label(_ artifact: ArtifactDoc) -> String {
        artifact.title.isEmpty ? artifact.kind : artifact.title
    }

    private static func bubble(_ artifact: ArtifactDoc) -> WorkspaceBubble {
        WorkspaceBubble(document: .artifact(artifact.kind), title: label(artifact), artifact: artifact)
    }
}

/// What a bubble's menu asks of the workspace (#603), one per `WorkspaceView`, so
/// a workspace pushed on another tab never acts on the Artifacts tab's pages.
@MainActor
@Observable
final class WorkspaceActions {
    /// Edit was chosen for this document: its page opens its edit form once loaded.
    var editRequest: WorkspaceDocument?
    /// Delete was chosen for this artifact: the confirmation asks about it.
    var deleteTarget: ArtifactDoc?
    /// The kind whose open page holds unsaved edits (the confirmation says they go too).
    var unsavedKind: String?

    /// Consumes the edit request when it is for `document`.
    func takeEditRequest(for document: WorkspaceDocument) -> Bool {
        guard editRequest == document else { return false }
        editRequest = nil
        return true
    }
}
