import SwiftUI
import XCTest
@testable import Loore

// Deleting an artifact (#603): the bubbles' long-press menus, the selection
// after a delete, the delete call, and the page for a deleted link. The web's
// counterpart is ArtifactsPage.delete.test.js.

/// `GET /api/artifacts/` as the backend answers it after #603: the built-in
/// placeholders and the digest with `deletable: false`, custom kinds with `true`.
private let listJSON = """
{"artifacts":[
 {"id":null,"kind":"predictions","title":"Predictions","content":"","created_at":null,"deletable":false},
 {"id":null,"kind":"ai_preferences","title":"AI Interaction Preferences","content":"","created_at":null,"deletable":false},
 {"id":11,"kind":"external_digest","title":"Saved References Digest","content":"x","created_at":"2026-11-04T08:00:00Z","deletable":false},
 {"id":12,"kind":"intentions","title":"Intentions","content":"x","created_at":"2026-11-03T08:00:00Z","deletable":false},
 {"id":13,"kind":"landlord-case","title":"Landlord Case","content":"x","created_at":"2026-10-28T08:00:00Z","deletable":true},
 {"id":14,"kind":"loore-development-map","title":"Loore Development Map","content":"x","created_at":"2026-10-05T08:00:00Z","deletable":true},
 {"id":15,"kind":"memory","title":"Memory","content":"x","created_at":"2026-11-05T08:00:00Z","deletable":false},
 {"id":16,"kind":"scratchpad","title":"Scratchpad","content":"x","created_at":"2026-11-05T08:00:00Z","deletable":false},
 {"id":17,"kind":"winter-solstice-gathering","title":"Winter Solstice Gathering","content":"x","created_at":"2026-10-30T08:00:00Z","deletable":true}
],"deleted_kinds":["old-notes"]}
"""

final class ArtifactMenuTests: XCTestCase {
    private func row() throws -> [WorkspaceBubble] {
        WorkspaceMenu.bubbles(try decode(ArtifactList.self, listJSON).artifacts)
    }

    private func items(_ title: String) throws -> [WorkspaceMenuItem] {
        let bubble = try XCTUnwrap(try row().first { $0.title == title }, title)
        return WorkspaceMenu.items(for: bubble)
    }

    func testTheRowKeepsTheWebsOrder() throws {
        XCTAssertEqual(try row().map(\.title), [
            "Profile", "Intentions", "Todo", "Predictions", "Memory", "Scratchpad", "AI Interaction Preferences",
            "Landlord Case", "Loore Development Map", "Saved References Digest", "Winter Solstice Gathering", "+",
        ])
    }

    func testCustomArtifactsGetEditAndDelete() throws {
        for title in ["Landlord Case", "Loore Development Map", "Winter Solstice Gathering"] {
            XCTAssertEqual(try items(title), [.edit, .delete], title)
        }
    }

    func testBuiltInsAndTheDigestGetEditOnly() throws {
        for title in ["Intentions", "Predictions", "Memory", "Scratchpad", "AI Interaction Preferences",
                      "Saved References Digest"] {
            XCTAssertEqual(try items(title), [.edit], title)
        }
    }

    func testProfileAndTodoGetEditOnlyAndThePlusNoMenu() throws {
        XCTAssertEqual(try items("Profile"), [.edit])
        XCTAssertEqual(try items("Todo"), [.edit])
        XCTAssertEqual(try items("+"), [])
    }

    func testAListWithoutDeletableOffersNoDelete() throws {
        // A backend from before #603: nothing says the kind can be deleted.
        let list = try decode(ArtifactList.self, #"{"artifacts":[{"id":3,"kind":"my-notes","title":"My Notes","content":"x"}]}"#)
        XCTAssertEqual(list.deletedKinds, [])
        let bubble = try XCTUnwrap(WorkspaceMenu.bubbles(list.artifacts).first { $0.document == .artifact("my-notes") })
        XCTAssertEqual(WorkspaceMenu.items(for: bubble), [.edit])
    }

    func testAnUntitledArtifactIsLabelledByItsKind() {
        let row = WorkspaceMenu.bubbles([ArtifactDoc(kind: "zeta-notes", title: "", deletable: true)])
        XCTAssertEqual(row.first { $0.document == .artifact("zeta-notes") }?.title, "zeta-notes")
    }

    // MARK: Selection after a delete

    func testDeletingTheOpenArtifactOpensTheNextBubble() throws {
        let next = WorkspaceMenu.selection(afterDeleting: "loore-development-map",
                                           active: .artifact("loore-development-map"), row: try row())
        XCTAssertEqual(next, .artifact("external_digest"))
    }

    func testDeletingTheLastOpensThePreviousNotThePlus() throws {
        let next = WorkspaceMenu.selection(afterDeleting: "winter-solstice-gathering",
                                           active: .artifact("winter-solstice-gathering"), row: try row())
        XCTAssertEqual(next, .artifact("external_digest"))
    }

    func testDeletingTheOnlyCustomArtifactOpensTheBuiltInBeforeIt() {
        let row = WorkspaceMenu.bubbles([ArtifactDoc(kind: "memory", title: "Memory"),
                                         ArtifactDoc(kind: "solo", title: "Solo", deletable: true)])
        XCTAssertEqual(WorkspaceMenu.selection(afterDeleting: "solo", active: .artifact("solo"), row: row),
                       .artifact("memory"))
    }

    func testDeletingABubbleThatIsNotOpenLeavesTheOpenOne() throws {
        XCTAssertNil(WorkspaceMenu.selection(afterDeleting: "landlord-case", active: .artifact("memory"), row: try row()))
        XCTAssertNil(WorkspaceMenu.selection(afterDeleting: "landlord-case", active: .profile, row: try row()))
    }

    // MARK: Edit requests

    @MainActor
    func testAnEditRequestIsTakenOnlyByItsDocumentAndOnce() {
        let actions = WorkspaceActions()
        actions.editRequest = .todo
        XCTAssertFalse(actions.takeEditRequest(for: .profile))
        XCTAssertEqual(actions.editRequest, .todo)
        XCTAssertTrue(actions.takeEditRequest(for: .todo))
        XCTAssertNil(actions.editRequest)
        XCTAssertFalse(actions.takeEditRequest(for: .todo))
    }

    // MARK: The confirmation

    func testTheDialogNamesTheArtifactInBoldAndSaysNothingOfTheGracePeriod() {
        let body = DeleteConfirmDialog.artifactBody(title: "Loore Development Map", unsavedEdits: false)
        XCTAssertEqual(String(body.characters),
                       "Loore Development Map and its version history will be removed. The AI will no longer see it.")
        let first = body.runs.first.map { String(body[$0.range].characters) }
        XCTAssertEqual(first, "Loore Development Map")
        XCTAssertEqual(body.runs.count, 2)
        XCTAssertFalse(String(body.characters).contains("30"))
    }

    func testTheDialogSaysWhenUnsavedEditsGoToo() {
        let body = DeleteConfirmDialog.artifactBody(title: "Notes", unsavedEdits: true)
        XCTAssertTrue(String(body.characters).hasSuffix("The AI will no longer see it. Your unsaved edits go with it."))
    }
}

@MainActor
final class ArtifactsStoreDeletionTests: StubbedAppTestCase {
    private var store: ArtifactsStore { app.artifacts }

    private func load() async {
        StubURLProtocol.install { _ in .json(200, listJSON) }
        await store.refresh(api: app.api)
        StubURLProtocol.reset()
    }

    func testDeleteSendsADeleteForTheKind() async {
        await load()
        StubURLProtocol.install { _ in .json(200, #"{"deleted":"landlord-case","versions":6,"grace_days":30}"#) }
        let result = await store.delete("landlord-case", api: app.api)
        XCTAssertEqual(result, .deleted)
        XCTAssertEqual(calls, ["DELETE /api/artifacts/landlord-case"])
        // The caller removes the bubble, with the next selection.
        XCTAssertNotNil(store.artifact("landlord-case"))
    }

    func testAKindDeletedOnAnotherDeviceCountsAsDone() async {
        StubURLProtocol.install { _ in .json(404, #"{"error":"This artifact was deleted.","deleted":true}"#) }
        let result = await store.delete("landlord-case", api: app.api)
        XCTAssertEqual(result, .deleted)
    }

    func testRefusalsKeepEverythingAndSayWhy() async {
        await load()
        StubURLProtocol.install { _ in .json(403, #"{"error":"Built-in artifacts can't be deleted."}"#) }
        var result = await store.delete("memory", api: app.api)
        XCTAssertEqual(result, .failed("Built-in artifacts can't be deleted."))
        StubURLProtocol.install { _ in .json(404, #"{"error":"Artifact not found"}"#) }
        result = await store.delete("nope", api: app.api)
        XCTAssertEqual(result, .failed("Artifact not found"))
        StubURLProtocol.install { _ in .html(500) }
        result = await store.delete("landlord-case", api: app.api)
        XCTAssertEqual(result, .failed("Could not delete the artifact."))
        StubURLProtocol.install { _ in StubResponse(status: 200, failWith: URLError(.notConnectedToInternet)) }
        result = await store.delete("landlord-case", api: app.api)
        XCTAssertEqual(result, .failed("Could not delete the artifact."))
        XCTAssertEqual(store.artifacts.count, 9)
        XCTAssertFalse(store.isDeleted("landlord-case"))
    }

    func testAfterTheDeleteTheBubbleIsGoneAndItsLinkSaysSo() async {
        await load()
        store.markDeleted("landlord-case")
        XCTAssertNil(store.artifact("landlord-case"))
        XCTAssertFalse(WorkspaceMenu.bubbles(store.artifacts).contains { $0.document == .artifact("landlord-case") })
        XCTAssertTrue(store.isDeleted("landlord-case"))
        XCTAssertNil(ArtifactsPage.resolve("landlord-case", in: store))
    }

    func testTheListsDeletedKindsMakeALinkSayDeleted() async {
        await load()
        XCTAssertEqual(store.deletedKinds, ["old-notes"])
        XCTAssertTrue(store.isDeleted("old-notes"))
        XCTAssertNil(ArtifactsPage.resolve("old-notes", in: store))
        // A listed kind opens as itself; an unused name opens empty under its name.
        XCTAssertEqual(ArtifactsPage.resolve("memory", in: store)?.rowId, 15)
        let unused = ArtifactsPage.resolve("fresh-idea", in: store)
        XCTAssertEqual(unused?.title, "Fresh Idea")
        XCTAssertEqual(unused?.exists, false)
    }

    func testAKindMadeAgainIsNotDeleted() async {
        await load()
        store.markDeleted("landlord-case")
        StubURLProtocol.install { _ in
            .json(200, #"{"artifacts":[{"id":40,"kind":"landlord-case","title":"Landlord Case","content":"new","created_at":"2026-11-06T09:00:00Z","deletable":true}],"deleted_kinds":[]}"#)
        }
        await store.refresh(api: app.api)
        XCTAssertFalse(store.isDeleted("landlord-case"))
        XCTAssertEqual(ArtifactsPage.resolve("landlord-case", in: store)?.rowId, 40)
    }

    func testAListFetchedBeforeTheDeleteDoesNotBringTheBubbleBack() async {
        await load()
        let freshList = listJSON
            .split(separator: "\n").filter { !$0.contains(#""kind":"landlord-case""#) }.joined(separator: "\n")
            .replacingOccurrences(of: #""deleted_kinds":["old-notes"]"#,
                                  with: #""deleted_kinds":["old-notes","landlord-case"]"#)
        var lists = [listJSON, freshList]
        let store = self.store
        StubURLProtocol.install { _ in
            // The first list was on its way when the delete landed.
            if lists.count == 2 {
                DispatchQueue.main.sync { MainActor.assumeIsolated { store.markDeleted("landlord-case") } }
            }
            return .json(200, lists.removeFirst())
        }
        await store.refresh(api: app.api)
        XCTAssertEqual(StubURLProtocol.requests.count, 2)
        XCTAssertTrue(store.isDeleted("landlord-case"))
    }
}

/// Edit chosen in the menu of a bubble that is not open (review of #605,
/// finding 1): the new page opens the editor on the server's current text, not
/// on the list cached from an earlier visit.
@MainActor
final class ArtifactEditFromMenuTests: StubbedAppTestCase {
    private static func list(_ content: String) -> String {
        #"{"artifacts":[{"id":7,"kind":"notes","title":"Notes","content":"\#(content)","created_at":"2026-11-01T08:00:00Z","deletable":true}],"deleted_kinds":[]}"#
    }

    private func textViews(in view: UIView) -> [UITextView] {
        ((view as? UITextView).map { [$0] } ?? []) + view.subviews.flatMap { textViews(in: $0) }
    }

    func testEditFromTheMenuStartsFromTheServersCurrentText() async throws {
        // The list as an earlier visit left it; the artifact changed on the server since.
        StubURLProtocol.install { _ in .json(200, Self.list("OLD")) }
        await app.artifacts.refresh(api: app.api)
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/artifacts/": return .json(200, Self.list("NEW"))
            case "/api/artifacts/notes/versions": return .json(200, #"{"versions":[]}"#)
            default: return .json(204, "")
            }
        }
        let actions = WorkspaceActions()
        actions.editRequest = .artifact("notes")
        let host = UIHostingController(rootView: ArtifactsPage(kind: "notes", creating: false, onSelect: { _ in })
            .environment(app)
            .environment(actions))
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 390, height: 844)
        window.rootViewController = host
        window.makeKeyAndVisible()
        defer { window.isHidden = true }

        var editor: UITextView?
        for _ in 0..<150 {
            try await Task.sleep(nanoseconds: 20_000_000)
            if actions.editRequest == nil, app.artifacts.artifact("notes")?.content == "NEW",
               let found = textViews(in: host.view).first {
                editor = found
                break
            }
        }
        try await Task.sleep(nanoseconds: 200_000_000) // a render after the refresh
        XCTAssertEqual(try XCTUnwrap(editor, "the edit form did not open").text, "NEW")
    }
}
