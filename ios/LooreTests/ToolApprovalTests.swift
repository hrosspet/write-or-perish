import XCTest
@testable import Loore

// Write tool calls that wait for the user's approval in a thread holding text
// they didn't write (Peter, 2026-11-12).

private let artifactItemJSON = """
{"id":31,"node_id":77,"turn_node_id":77,"tool":"update_artifact","kinds":["other_user","glean"],
 "created_at":"2026-11-12T10:00:00Z","title":"Change your Memory","stale":false,
 "kind":"memory","artifact_title":"Memory","created":false,"base_version":3,"current_version":3,
 "lines":[{"type":"same","text":"Rivers"},{"type":"del","text":"old line"},{"type":"add","text":"new line"}]}
"""
private let todoItemJSON = """
{"id":32,"node_id":77,"turn_node_id":77,"tool":"apply_todo_changes","kinds":["saved_reference"],
 "created_at":"2026-11-12T10:00:01Z","title":"Apply the proposed changes to your todo list","stale":false,
 "proposal_node_id":76,"proposal_text":"### New Tasks\\n- [ ] Walk by the river"}
"""

final class ToolApprovalDecodingTests: XCTestCase {
    func testAnArtifactWriteDecodesWithItsDiff() throws {
        let item = try decode(ToolApproval.self, artifactItemJSON)
        XCTAssertEqual(item.id, 31)
        XCTAssertEqual(item.turnNodeId, 77)
        XCTAssertTrue(item.isArtifactWrite)
        XCTAssertEqual(item.kinds, [.otherUser, .glean])
        XCTAssertEqual(item.title, "Change your Memory")
        XCTAssertEqual(item.artifactTitle, "Memory")
        XCTAssertEqual(item.lines.map(\.type), [.same, .del, .add])
        XCTAssertEqual(item.lines.map(\.text), ["Rivers", "old line", "new line"])
        XCTAssertEqual(item.versionNote, "Based on v3")
        XCTAssertNil(item.staleNote)
        XCTAssertTrue(item.canApprove)
        XCTAssertNotNil(item.createdAt)
        XCTAssertEqual(VersionDiff(lines: item.lines).changedCount, 2)
    }

    func testAStaleArtifactWriteCannotBeApproved() throws {
        let json = artifactItemJSON.replacingOccurrences(of: #""stale":false"#, with: #""stale":true"#)
            .replacingOccurrences(of: #""current_version":3"#, with: #""current_version":5"#)
        let item = try decode(ToolApproval.self, json)
        XCTAssertEqual(item.staleNote, "Your Memory changed since (now v5). Approving would save nothing.")
        XCTAssertFalse(item.canApprove)
    }

    func testTitleAndDescriptionChangesDecodeIntoTheirLines() throws {
        let json = artifactItemJSON.replacingOccurrences(of: #""stale":false,"#, with: """
        "stale":false,"title_change":{"from":"Memory","to":"Shared memory"},
         "description_change":{"from":null,"to":"What I keep"},
        """)
        let item = try decode(ToolApproval.self, json)
        XCTAssertEqual(item.titleChange, .init(from: "Memory", to: "Shared memory"))
        XCTAssertEqual(item.descriptionChange, .init(from: nil, to: "What I keep"))
        XCTAssertEqual(item.fieldChangeLines, ["Title: Memory → Shared memory", "Description: What I keep"])
        XCTAssertEqual(item.lines.count, 3, "the text's diff stays")

        // Only what the call changes; none when it changes only the text.
        let onlyDescription = ToolApproval(id: 1, turnNodeId: 2, tool: "update_artifact", title: "x",
                                           descriptionChange: .init(from: "Old", to: "New"))
        XCTAssertEqual(onlyDescription.fieldChangeLines, ["Description: Old → New"])
        XCTAssertEqual(try decode(ToolApproval.self, artifactItemJSON).fieldChangeLines, [])
    }

    func testAnIssueItemWithUnknownKeysDecodes() throws {
        let item = try decode(ToolApproval.self, """
        {"id":50,"turn_node_id":77,"tool":"apply_github_issue","kinds":["other_user"],"stale":false,
         "title":"File the proposed GitHub issue: Crash on save","proposal_node_id":76,
         "proposal_text":"### Issue\\nCrash on save","issues":[{"number":1,"title":"Crash on save"}],
         "something_new":{"nested":[1,2]}}
        """)
        XCTAssertEqual(item.title, "File the proposed GitHub issue: Crash on save")
        XCTAssertEqual(item.proposalText, "### Issue\nCrash on save")
        XCTAssertEqual(item.fieldChangeLines, [])
    }

    func testANewArtifactHasNoBaseVersion() throws {
        let item = try decode(ToolApproval.self, """
        {"id":40,"turn_node_id":77,"tool":"update_artifact","title":"Create your Reading List","created":true,
         "kind":"reading-list","artifact_title":"Reading List","base_version":null,"current_version":null,
         "stale":false,"lines":[{"type":"add","text":"Book"}]}
        """)
        XCTAssertTrue(item.created)
        XCTAssertNil(item.versionNote)
        XCTAssertEqual(item.lines.map(\.type), [.add])
    }

    func testAnApplyCallDecodesWithItsProposal() throws {
        let item = try decode(ToolApproval.self, todoItemJSON)
        XCTAssertFalse(item.isArtifactWrite)
        XCTAssertEqual(item.kinds, [.savedReference])
        XCTAssertEqual(item.proposalNodeId, 76)
        XCTAssertEqual(item.proposalText, "### New Tasks\n- [ ] Walk by the river")
        XCTAssertEqual(item.lines, [])
        XCTAssertNil(item.versionNote)

        let stale = try decode(ToolApproval.self, #"{"id":33,"turn_node_id":77,"tool":"apply_share","title":"Save the proposed share as a private draft","stale":true}"#)
        XCTAssertEqual(stale.staleNote, "Already applied or replaced.")
        XCTAssertFalse(stale.canApprove)
    }

    func testTheListSkipsAnItemThatDoesNotDecode() throws {
        let list = try decode(ToolApprovalList.self, #"{"pending":[\#(todoItemJSON),{"tool":"apply_feedback"}]}"#)
        XCTAssertEqual(list.pending.map(\.id), [32])
    }

    func testTheNodeCarriesItsWaitingCalls() throws {
        let decoder = APIClient.makeDecoder()
        let with = try NodeTreeDecoding.decodeNodeDetail(Data("""
        {"id":10,"content":"entry","children":[{"id":77,"content":"reply","children":[]}],
         "pending_approvals":[\(artifactItemJSON),\(todoItemJSON)]}
        """.utf8), decoder: decoder)
        XCTAssertEqual(with.pendingApprovals.map(\.id), [31, 32])
        XCTAssertEqual(with.children.map(\.id), [77])
        let without = try NodeTreeDecoding.decodeNodeDetail(Data(#"{"id":10,"content":"entry"}"#.utf8), decoder: decoder)
        XCTAssertEqual(without.pendingApprovals, [])
    }

    func testTheDecisionAnswerDecodes() throws {
        let approved = try decode(ToolApprovalDecision.self, #"{"outcome":"approved","pending":[\#(todoItemJSON)]}"#)
        XCTAssertEqual(approved.outcome, ToolApprovalDecision.approved)
        XCTAssertEqual(approved.pending.map(\.id), [32])
        XCTAssertNil(approved.error)
        let failed = try decode(ToolApprovalDecision.self, #"{"outcome":"failed","pending":[],"error":"The todo list changed."}"#)
        XCTAssertEqual(failed.outcome, ToolApprovalDecision.failed)
        XCTAssertEqual(failed.error, "The todo list changed.")
    }

    func testCallsAreGroupedByReplyOldestFirst() {
        let items = [
            ToolApproval(id: 5, turnNodeId: 90, tool: "update_artifact", title: "a"),
            ToolApproval(id: 6, turnNodeId: 77, tool: "apply_todo_changes", title: "b"),
            ToolApproval(id: 7, turnNodeId: 90, tool: "apply_share", title: "c"),
            ToolApproval(id: 8, turnNodeId: 77, tool: "update_artifact", title: "d"),
        ]
        let groups = ToolApprovalGroup.groups(items)
        XCTAssertEqual(groups.map(\.turnNodeId), [77, 90])
        XCTAssertEqual(groups.map { $0.items.map(\.id) }, [[6, 8], [5, 7]])
        XCTAssertEqual(ToolApprovalGroup.groups([]), [])
    }

    func testTheReasonNamesTheKindsOfText() {
        func reason(_ kinds: [ForeignTextKind]) -> String { ToolApprovalGroup.reason(kinds) }
        XCTAssertEqual(reason([.otherUser]),
                       "This thread holds text you didn't write (another person's writing), so Loore asks before the AI changes your data.")
        XCTAssertEqual(reason([.glean, .savedReference, .glean]),
                       "This thread holds text you didn't write (a saved reference and tweets from Glean), so Loore asks before the AI changes your data.")
        XCTAssertEqual(reason([.glean, .otherUser, .savedReference]),
                       "This thread holds text you didn't write (another person's writing, a saved reference and tweets from Glean), so Loore asks before the AI changes your data.")
        XCTAssertEqual(reason([.unknown("future")]),
                       "This thread holds text you didn't write, so Loore asks before the AI changes your data.")
        let group = ToolApprovalGroup(turnNodeId: 77, items: [
            ToolApproval(id: 1, turnNodeId: 77, tool: "update_artifact", kinds: [.glean], title: "a"),
            ToolApproval(id: 2, turnNodeId: 77, tool: "apply_share", kinds: [.otherUser], title: "b"),
        ])
        XCTAssertEqual(group.reason, reason([.otherUser, .glean]))
    }

    func testActionsTakenLabelsForWaitingAndDeclinedCalls() {
        let waiting = ToolCallMeta(name: "update_artifact", raw: ["status": "pending_approval", "pending_id": 31, "kind": "memory"])
        XCTAssertEqual(waiting.approvalLabel(), "Waiting for your approval")
        XCTAssertEqual(waiting.approvalLabel(owner: false), "Waiting for approval")
        XCTAssertEqual(waiting.approvalActionName, "Artifact change")
        let declined = ToolCallMeta(name: "apply_todo_changes", raw: ["status": "rejected", "approval": "rejected"])
        XCTAssertEqual(declined.approvalLabel(), "Declined")
        XCTAssertEqual(declined.approvalActionName, "Todo changes")
        let approved = ToolCallMeta(name: "update_artifact", raw: ["status": "success", "approval": "approved"])
        XCTAssertNil(approved.approvalLabel())
        XCTAssertNil(ToolCallMeta(name: "semantic_search", raw: ["status": "success"]).approvalLabel())
    }
}

/// The dialog's model against a stubbed backend.
@MainActor
final class ToolApprovalsModelTests: StubbedAppTestCase {
    private func items(_ json: String...) throws -> [ToolApproval] {
        try json.map { try decode(ToolApproval.self, $0) }
    }

    private func model() -> ToolApprovalsModel {
        let m = ToolApprovalsModel(api: app.api, nodeId: 10)
        m.autoOpenDelay = 0
        return m
    }

    private func eventually(_ condition: @escaping () -> Bool) async -> Bool {
        for _ in 0..<40 {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    func testItOpensByItselfOnTheOldestReplyAndLaterDecidesNothing() async throws {
        let m = model()
        let older = try decode(ToolApproval.self, #"{"id":1,"turn_node_id":50,"tool":"apply_share","title":"x"}"#)
        m.update(try items(artifactItemJSON) + [older], autoOpen: true)
        let opened = await eventually { m.shownTurnId != nil }
        XCTAssertTrue(opened)
        XCTAssertEqual(m.shownGroup?.turnNodeId, 50)
        XCTAssertEqual(m.waitingLine, "2 changes wait for your approval")
        m.later()
        XCTAssertNil(m.shownGroup)
        XCTAssertEqual(m.pending.count, 2, "still waiting")
        XCTAssertTrue(calls.isEmpty, "nothing sent")
        m.review()
        XCTAssertEqual(m.shownTurnId, 50)
        XCTAssertEqual(ToolApprovalsModel.waitingLine(count: 1), "1 change waits for your approval")
    }

    func testWithoutAutoOpenItWaitsForReview() async throws {
        let m = model()
        m.update(try items(artifactItemJSON), autoOpen: false)
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertNil(m.shownTurnId)
        XCTAssertNotNil(m.waitingLine)
    }

    func testEachAnswerReplacesTheReplysCallsAndTheLastClosesTheDialog() async throws {
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/tool-approvals/31/approve": return .json(200, #"{"outcome":"approved","pending":[\#(todoItemJSON)]}"#)
            case "/api/tool-approvals/32/reject": return .json(200, #"{"outcome":"rejected","pending":[]}"#)
            default: return .json(500, "{}")
            }
        }
        let m = model()
        var approved: [Int] = []
        var done = 0
        m.onApproved = { approved.append($0.id) }
        m.onGroupDone = { done += 1 }
        let list = try items(artifactItemJSON, todoItemJSON)
        m.update(list, autoOpen: false)
        m.review()
        await m.decide(list[0], approve: true)
        XCTAssertEqual(m.shownGroup?.items.map(\.id), [32])
        XCTAssertEqual(approved, [31])
        XCTAssertEqual(done, 0)
        await m.decide(list[1], approve: false)
        XCTAssertNil(m.shownTurnId)
        XCTAssertTrue(m.pending.isEmpty)
        XCTAssertNil(m.waitingLine)
        XCTAssertEqual(approved, [31], "a rejection runs nothing")
        XCTAssertEqual(done, 1)
        XCTAssertTrue(m.busy.isEmpty)
    }

    func testAFailedRunShowsItsErrorAndTheCallStillWaits() async throws {
        StubURLProtocol.install { _ in
            .json(200, #"{"outcome":"failed","pending":[\#(artifactItemJSON)],"error":"Your Memory changed since this was proposed."}"#)
        }
        let m = model()
        let list = try items(artifactItemJSON)
        m.update(list, autoOpen: false)
        m.review()
        await m.decide(list[0], approve: true)
        XCTAssertEqual(m.errors[31], "Your Memory changed since this was proposed.")
        XCTAssertEqual(m.shownGroup?.items.map(\.id), [31])
    }

    func testAnAlreadyDecidedCallIsDroppedSilently() async throws {
        StubURLProtocol.install { request in
            if request.url?.path(percentEncoded: true) == "/api/tool-approvals" {
                return .json(200, #"{"pending":[\#(todoItemJSON)]}"#)
            }
            return .json(404, #"{"error":"Not found"}"#)
        }
        let m = model()
        let list = try items(artifactItemJSON, todoItemJSON)
        m.update(list, autoOpen: false)
        m.review()
        await m.decide(list[0], approve: true)
        XCTAssertEqual(m.pending.map(\.id), [32])
        XCTAssertNil(m.errors[31])
        XCTAssertEqual(m.shownTurnId, 77, "the reply's other call still waits")
        XCTAssertTrue(calls.contains("GET /api/tool-approvals"))
        XCTAssertEqual(StubURLProtocol.requests.last?.url?.query, "node=10")
        XCTAssertTrue(app.toasts.toasts.isEmpty)
    }

    func testAServerErrorKeepsTheCallWithTheMessage() async throws {
        StubURLProtocol.install { _ in
            .json(500, #"{"error":"Loore couldn't make this change. It still waits for your approval."}"#)
        }
        let m = model()
        let list = try items(todoItemJSON)
        m.update(list, autoOpen: false)
        m.review()
        await m.decide(list[0], approve: true)
        XCTAssertEqual(m.errors[32], ToolApprovalsModel.notRunMessage)
        XCTAssertEqual(m.pending.map(\.id), [32])
        XCTAssertEqual(m.shownTurnId, 77)
    }

    func testAStaleCallIsNotSentForApproval() async throws {
        let m = model()
        let stale = try decode(ToolApproval.self, #"{"id":33,"turn_node_id":77,"tool":"apply_share","title":"x","stale":true}"#)
        m.update([stale], autoOpen: false)
        await m.decide(stale, approve: true)
        XCTAssertTrue(calls.isEmpty)
    }

    // MARK: On the thread screen

    func testTheThreadOpensTheDialogOnItsFirstLoadOnly() async throws {
        let focal = """
        {"id":10,"content":"entry","node_type":"user","user":{"id":5,"username":"seowriter"},"privacy_level":"private",
         "ai_usage":"chat","children":[],"pending_approvals":[\(artifactItemJSON)]}
        """
        StubURLProtocol.install { _ in .json(200, focal) }
        let thread = ThreadModel(nodeId: 10, awaitLLM: nil, app: app)
        thread.approvals.autoOpenDelay = 0
        await thread.load()
        let opened = await eventually { thread.approvals.shownTurnId == 77 }
        XCTAssertTrue(opened)
        thread.approvals.later()
        await thread.load()
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertNil(thread.approvals.shownTurnId, "a reload does not ask again")
        XCTAssertEqual(thread.approvals.pending.map(\.id), [31])
    }

    func testAFinishedReplyAsksForItsWaitingCalls() async throws {
        let pending = """
        {"id":77,"content":"[LLM response generation pending...]","node_type":"llm","llm_model":"gpt-6-luna",
         "llm_task_status":"pending","user":{"id":99,"username":"gpt-6-luna"},"parent_user_id":5,
         "privacy_level":"private","ai_usage":"chat",
         "ancestors":[{"id":10,"content":"entry","node_type":"user","user_id":5,"ai_usage":"chat","privacy_level":"private"}]}
        """
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/nodes/77": return .json(200, pending)
            case "/api/nodes/77/llm-status": return .json(200, #"{"node_id":77,"status":"completed","content":"Done."}"#)
            case "/api/tool-approvals": return .json(200, #"{"pending":[\#(artifactItemJSON)]}"#)
            default: return .json(200, "{}")
            }
        }
        let tab = app.router.selectedTab
        app.router.setPath([.thread(id: 10, awaitLLM: nil), .thread(id: 77, awaitLLM: 77)], for: tab)
        let thread = ThreadModel(nodeId: 77, awaitLLM: 77, app: app)
        thread.approvals.autoOpenDelay = 0
        await thread.start()
        let asked = await eventually { thread.approvals.shownTurnId == 77 }
        XCTAssertTrue(asked)
        XCTAssertTrue(calls.contains("GET /api/tool-approvals"))
        thread.stop()
    }
}
