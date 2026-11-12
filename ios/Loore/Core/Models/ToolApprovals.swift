import Foundation

// Write tool calls that wait for the user's approval (Peter, 2026-11-12):
// "direct writing to artifacts should be in the case when other user's text is
// in the context, intercepted and via a dialog raised to the user. It should
// [show] what tool call is proposed, and show the proposed diff if it's about
// changing an artifact. The user would have to approve every single tool call
// in such a thread. If more the one such possibly destructive tool call is made
// in a single node, all of them should be raised inside one dialog". Nothing is
// written until the user approves; dismissing the dialog decides nothing.
// Server: backend/utils/tool_approvals.py, backend/routes/tool_approvals.py.

/// Where the text the user didn't write came from (an item's `kinds`).
enum ForeignTextKind: OpenEnum {
    case otherUser, savedReference, glean, unknown(String)

    init(rawString: String) {
        switch rawString {
        case "other_user": self = .otherUser
        case "saved_reference": self = .savedReference
        case "glean": self = .glean
        default: self = .unknown(rawString)
        }
    }

    var rawString: String {
        switch self {
        case .otherUser: return "other_user"
        case .savedReference: return "saved_reference"
        case .glean: return "glean"
        case .unknown(let s): return s
        }
    }
}

/// One waiting call (`pending_approvals` on `GET /api/nodes/<id>`,
/// `GET /api/tool-approvals?node=<id>`, and a decision's `pending`). Two shapes:
/// `update_artifact` carries the artifact and the line diff of what it would
/// save; the `apply_*` tools carry the proposal they would apply.
struct ToolApproval: Decodable, Identifiable, Equatable, Sendable {
    var id: Int
    /// The reply the call was made in.
    var nodeId: Int?
    /// The reply's turn: one dialog per turn.
    var turnNodeId: Int
    /// "update_artifact" | "apply_todo_changes" | "apply_github_issue" | "apply_feedback" | "apply_share"
    var tool: String
    var kinds: [ForeignTextKind]
    var createdAt: Date?
    /// "Change your Memory", "Apply the proposed changes to your todo list", …
    var title: String
    /// Approving would do nothing now (the artifact changed; the proposal was applied or replaced).
    var stale: Bool

    // update_artifact
    var kind: String?
    var artifactTitle: String?
    var created: Bool
    var baseVersion: Int?
    var currentVersion: Int?
    /// From the version the call was based on to the text it would save.
    var lines: [LineDiff.Op]
    /// What the call changes besides the text; each only when it changes it.
    var titleChange: FieldChange?
    var descriptionChange: FieldChange?
    /// The write could not be prepared (shown instead of the diff).
    var error: String?

    // apply_*
    var proposalNodeId: Int?
    /// The text of the reply that proposed it.
    var proposalText: String?

    static let artifactTool = "update_artifact"

    var isArtifactWrite: Bool { tool == Self.artifactTool }

    /// `title_change` / `description_change`: `from` is nil when there was none.
    struct FieldChange: Decodable, Equatable, Sendable {
        var from: String?
        var to: String

        init(from: String?, to: String) {
            self.from = from
            self.to = to
        }

        enum CodingKeys: String, CodingKey { case from, to }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            from = c.tolerant(.from)
            to = c.tolerant(.to, default: "")
        }

        /// "Title: <from> → <to>", or "Title: <to>" when there was none.
        func line(_ label: String) -> String {
            guard let from else { return "\(label): \(to)" }
            return "\(label): \(from) → \(to)"
        }
    }

    /// The dialog's lines above the diff: what changes besides the text.
    var fieldChangeLines: [String] {
        [titleChange?.line("Title"), descriptionChange?.line("Description")].compactMap { $0 }
    }

    /// "Based on vN" for a change of an existing artifact.
    var versionNote: String? {
        guard isArtifactWrite, let baseVersion else { return nil }
        return "Based on v\(baseVersion)"
    }

    /// Why Approve is off, when it is.
    var staleNote: String? {
        guard stale else { return nil }
        guard isArtifactWrite else { return "Already applied or replaced." }
        let name = artifactTitle ?? kind ?? "artifact"
        let now = currentVersion.map { " (now v\($0))" } ?? ""
        return "Your \(name) changed since\(now). Approving would save nothing."
    }

    var canApprove: Bool { !stale }

    init(id: Int, nodeId: Int? = nil, turnNodeId: Int, tool: String, kinds: [ForeignTextKind] = [],
         createdAt: Date? = nil, title: String, stale: Bool = false, kind: String? = nil,
         artifactTitle: String? = nil, created: Bool = false, baseVersion: Int? = nil,
         currentVersion: Int? = nil, lines: [LineDiff.Op] = [], titleChange: FieldChange? = nil,
         descriptionChange: FieldChange? = nil, error: String? = nil,
         proposalNodeId: Int? = nil, proposalText: String? = nil) {
        self.id = id
        self.nodeId = nodeId
        self.turnNodeId = turnNodeId
        self.tool = tool
        self.kinds = kinds
        self.createdAt = createdAt
        self.title = title
        self.stale = stale
        self.kind = kind
        self.artifactTitle = artifactTitle
        self.created = created
        self.baseVersion = baseVersion
        self.currentVersion = currentVersion
        self.lines = lines
        self.titleChange = titleChange
        self.descriptionChange = descriptionChange
        self.error = error
        self.proposalNodeId = proposalNodeId
        self.proposalText = proposalText
    }

    private struct WireLine: Decodable {
        var type: String
        var text: String

        enum CodingKeys: String, CodingKey { case type, text }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            type = c.tolerant(.type, default: "same")
            text = c.tolerant(.text, default: "")
        }
    }

    enum CodingKeys: String, CodingKey {
        case id, tool, kinds, title, stale, kind, created, lines, error
        case nodeId = "node_id"
        case turnNodeId = "turn_node_id"
        case createdAt = "created_at"
        case artifactTitle = "artifact_title"
        case baseVersion = "base_version"
        case currentVersion = "current_version"
        case proposalNodeId = "proposal_node_id"
        case proposalText = "proposal_text"
        case titleChange = "title_change"
        case descriptionChange = "description_change"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        nodeId = c.tolerant(.nodeId)
        let turn: Int? = c.tolerant(.turnNodeId)
        turnNodeId = turn ?? nodeId ?? id
        tool = c.tolerant(.tool, default: "")
        kinds = c.tolerant(.kinds, default: [])
        createdAt = c.tolerant(.createdAt)
        stale = c.tolerant(.stale, default: false)
        kind = c.tolerant(.kind)
        artifactTitle = c.tolerant(.artifactTitle)
        created = c.tolerant(.created, default: false)
        baseVersion = c.tolerant(.baseVersion)
        currentVersion = c.tolerant(.currentVersion)
        lines = c.tolerant(.lines, default: [WireLine]()).map {
            LineDiff.Op(type: LineDiff.Kind(rawValue: $0.type) ?? .same, text: $0.text)
        }
        titleChange = c.tolerant(.titleChange)
        descriptionChange = c.tolerant(.descriptionChange)
        error = c.tolerant(.error)
        proposalNodeId = c.tolerant(.proposalNodeId)
        proposalText = c.tolerant(.proposalText)
        let wireTitle: String? = c.tolerant(.title)
        title = wireTitle ?? tool
    }

    /// A list of items, skipping any that does not decode.
    static func decodeList<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> [ToolApproval] {
        c.tolerant(key, default: [LossyApproval]()).compactMap(\.value)
    }

    private struct LossyApproval: Decodable {
        var value: ToolApproval?
        init(from decoder: Decoder) throws { value = try? ToolApproval(from: decoder) }
    }
}

/// `GET /api/tool-approvals?node=<id>`.
struct ToolApprovalList: Decodable, Sendable {
    var pending: [ToolApproval]

    enum CodingKeys: String, CodingKey { case pending }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        pending = ToolApproval.decodeList(c, .pending)
    }
}

/// `POST /api/tool-approvals/<id>/approve|reject` (200). `failed`: nothing was
/// saved and the call still waits; `error` says why. `pending`: the same reply's
/// calls still waiting.
struct ToolApprovalDecision: Decodable, Sendable {
    var outcome: String
    var pending: [ToolApproval]
    var error: String?

    static let approved = "approved"
    static let rejected = "rejected"
    static let failed = "failed"

    enum CodingKeys: String, CodingKey { case outcome, pending, error }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        outcome = c.tolerant(.outcome, default: "")
        pending = ToolApproval.decodeList(c, .pending)
        error = c.tolerant(.error)
    }
}

/// The calls one reply made: one dialog.
struct ToolApprovalGroup: Identifiable, Equatable, Sendable {
    var turnNodeId: Int
    var items: [ToolApproval]

    var id: Int { turnNodeId }

    /// Grouped by the reply that made them, the oldest reply first (node ids
    /// grow with time); each group keeps the server's order (oldest call first).
    static func groups(_ items: [ToolApproval]) -> [ToolApprovalGroup] {
        var order: [Int] = []
        var byTurn: [Int: [ToolApproval]] = [:]
        for item in items {
            if byTurn[item.turnNodeId] == nil { order.append(item.turnNodeId) }
            byTurn[item.turnNodeId, default: []].append(item)
        }
        return order.sorted().map { ToolApprovalGroup(turnNodeId: $0, items: byTurn[$0] ?? []) }
    }

    /// The dialog's sentence on why it asks, from the kinds of text in the thread.
    var reason: String { Self.reason(items.flatMap(\.kinds)) }

    static func reason(_ kinds: [ForeignTextKind]) -> String {
        let names: [(ForeignTextKind, String)] = [
            (.otherUser, "another person's writing"),
            (.savedReference, "a saved reference"),
            (.glean, "tweets from Glean"),
        ]
        let present = names.filter { kinds.contains($0.0) }.map(\.1)
        let list: String
        switch present.count {
        case 0: list = ""
        case 1: list = present[0]
        default: list = present.dropLast().joined(separator: ", ") + " and " + present[present.count - 1]
        }
        let source = list.isEmpty ? "" : " (\(list))"
        return "This thread holds text you didn't write\(source), so Loore asks before the AI changes your data."
    }
}

extension ToolCallMeta {
    /// A call kept for the user's approval, or one they declined (Actions taken).
    static let pendingApprovalStatus = "pending_approval"
    static let rejectedStatus = "rejected"

    /// "Waiting for your approval" / "Declined", else nil (a normal entry).
    /// `owner` false: someone else reads the reply, so not "your".
    func approvalLabel(owner: Bool = true) -> String? {
        switch status {
        case Self.pendingApprovalStatus?: return owner ? "Waiting for your approval" : "Waiting for approval"
        case Self.rejectedStatus?: return "Declined"
        default: return nil
        }
    }

    /// What the call would do, named without saying it was done (rows whose
    /// `approvalLabel` is set).
    var approvalActionName: String {
        switch name {
        case "update_artifact": return "Artifact change"
        case "apply_todo_changes": return "Todo changes"
        case "apply_github_issue": return "GitHub issue"
        case "apply_feedback": return "Feedback"
        case "apply_share": return "Share draft"
        default: return name.isEmpty ? "Action" : name
        }
    }
}
