import SwiftUI
import Observation

/// The thread screen's write tool calls waiting for the user's approval (Peter,
/// 2026-11-12; models in `Core/Models/ToolApprovals.swift`). One dialog per reply,
/// the oldest reply first. It opens by itself once when the screen loads (or a
/// reply on it finishes) with calls waiting, and again from the waiting line.
/// "Later" decides nothing: the calls keep waiting.
@MainActor
@Observable
final class ToolApprovalsModel {
    @ObservationIgnored private let api: APIClient
    @ObservationIgnored let nodeId: Int
    /// Before the dialog opens by itself: lets the page's push finish first.
    @ObservationIgnored var autoOpenDelay: Double = 0.6
    /// A call was approved and run (the host refreshes what it wrote).
    @ObservationIgnored var onApproved: (ToolApproval) -> Void = { _ in }
    /// Every call of the dialog's reply was decided: the dialog closed.
    @ObservationIgnored var onGroupDone: () -> Void = {}

    /// Every waiting call in the thread, as the server listed them.
    private(set) var pending: [ToolApproval] = []
    /// The reply whose calls the dialog shows; nil when it is closed.
    private(set) var shownTurnId: Int?
    /// Calls with a request running: their buttons are off.
    private(set) var busy: Set<Int> = []
    /// Per call: why the last answer did not decide it.
    private(set) var errors: [Int: String] = [:]
    @ObservationIgnored private var autoOpenTask: Task<Void, Never>?

    static let notRunMessage = "Loore couldn't make this change. It still waits for your approval."

    init(api: APIClient, nodeId: Int) {
        self.api = api
        self.nodeId = nodeId
    }

    var groups: [ToolApprovalGroup] { ToolApprovalGroup.groups(pending) }

    var shownGroup: ToolApprovalGroup? {
        guard let turn = shownTurnId else { return nil }
        return groups.first { $0.turnNodeId == turn }
    }

    /// "2 changes wait for your approval" while any wait.
    var waitingLine: String? { pending.isEmpty ? nil : Self.waitingLine(count: pending.count) }

    static func waitingLine(count: Int) -> String {
        count == 1 ? "1 change waits for your approval" : "\(count) changes wait for your approval"
    }

    /// The thread's waiting calls (from the node's `pending_approvals` or the list).
    /// `autoOpen`: the screen loaded, or a reply finished, so the dialog opens by itself.
    func update(_ items: [ToolApproval], autoOpen: Bool) {
        pending = items
        errors = errors.filter { id, _ in items.contains { $0.id == id } }
        if let turn = shownTurnId, !items.contains(where: { $0.turnNodeId == turn }) {
            shownTurnId = nil
        }
        if autoOpen && !items.isEmpty { scheduleAutoOpen() }
    }

    /// `GET /api/tool-approvals?node=<id>`: the calls are made while a reply is
    /// generated, so a finished reply asks again. A failure keeps the list.
    func refresh(autoOpen: Bool) async {
        guard let list: ToolApprovalList = try? await api.get(APIPath.toolApprovals, query: [
            URLQueryItem(name: "node", value: String(nodeId)),
        ]) else { return }
        update(list.pending, autoOpen: autoOpen)
    }

    private func scheduleAutoOpen() {
        autoOpenTask?.cancel()
        let delay = autoOpenDelay
        autoOpenTask = Task { [weak self] in
            if delay > 0 { try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
            guard !Task.isCancelled, let self, self.shownTurnId == nil else { return }
            self.shownTurnId = self.groups.first?.turnNodeId
        }
    }

    /// The waiting line's Review: the oldest reply's calls.
    func review() {
        shownTurnId = groups.first?.turnNodeId
    }

    /// "Later" (or a tap outside): nothing is decided.
    func later() {
        autoOpenTask?.cancel()
        shownTurnId = nil
    }

    /// The screen went away: no dialog opens over another one.
    func stop() {
        autoOpenTask?.cancel()
        autoOpenTask = nil
    }

    /// Approve or reject one call. The answer's `pending` replaces the reply's
    /// calls; when none are left the dialog closes and the host refreshes the thread.
    func decide(_ item: ToolApproval, approve: Bool) async {
        guard !busy.contains(item.id), approve ? item.canApprove : true else { return }
        busy.insert(item.id)
        errors[item.id] = nil
        let turn = item.turnNodeId
        do {
            let path = approve ? APIPath.toolApprovalApprove(item.id) : APIPath.toolApprovalReject(item.id)
            let answer: ToolApprovalDecision = try await api.post(path)
            busy.remove(item.id)
            pending = pending.filter { $0.turnNodeId != turn } + answer.pending
            if answer.outcome == ToolApprovalDecision.failed {
                // Nothing was saved; the call still waits.
                errors[item.id] = answer.error ?? Self.notRunMessage
            } else if answer.outcome == ToolApprovalDecision.approved {
                onApproved(item)
            }
        } catch let error as APIError where error.status == 404 {
            // Already decided (another device, a double tap), or not theirs.
            busy.remove(item.id)
            pending.removeAll { $0.id == item.id }
            await refresh(autoOpen: false)
        } catch {
            busy.remove(item.id)
            errors[item.id] = (error as? APIError)?.serverMessage ?? Self.notRunMessage
            return
        }
        if shownTurnId == turn, !pending.contains(where: { $0.turnNodeId == turn }) {
            shownTurnId = nil
            onGroupDone()
        }
    }
}

/// "2 changes wait for your approval · Review" on the thread screen.
struct ToolApprovalsLine: View {
    let model: ToolApprovalsModel

    var body: some View {
        if let line = model.waitingLine {
            Button(action: model.review) {
                (Text(line + " · ").foregroundColor(LooreColor.textMuted)
                 + Text("Review").foregroundColor(LooreColor.accent))
                    .font(LooreFont.sans(13.1, .light))
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, minHeight: 32, alignment: .leading)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(line)
            .accessibilityHint("Opens the changes to approve or reject")
            .accessibilityIdentifier("approvals.line")
        }
    }
}

/// "Approve changes?": every waiting call of one reply, each with Approve and Reject.
/// Each call's buttons sit right under its title, above what it changes, so the
/// first call's are on screen when the dialog opens, whatever its diff (an
/// iPhone SE's 667 pt included); the heights below scale with the screen.
struct ToolApprovalDialog: View {
    let group: ToolApprovalGroup
    let model: ToolApprovalsModel
    /// The screen's height (a parameter so renders can take other phones').
    var screenHeight: CGFloat = UIScreen.main.bounds.height

    /// The calls' list, scrolled inside the dialog (heuristic: half the screen, so
    /// the whole card fits an iPhone SE's safe area with the four-line reason).
    static func listMaxHeight(screen: CGFloat) -> CGFloat { screen * 0.5 }
    /// A diff box (heuristic: 22 % of the screen, 120–240 pt).
    static func diffMaxHeight(screen: CGFloat) -> CGFloat { min(240, max(120, screen * 0.22)) }
    /// A proposal box (heuristic: 20 % of the screen, 100–180 pt).
    static func proposalMaxHeight(screen: CGFloat) -> CGFloat { min(180, max(100, screen * 0.2)) }

    var body: some View {
        LooreDialogCard(title: "Approve changes?") {
            DialogBodyText(text: group.reason)
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(group.items) { item in
                        ToolApprovalItemView(item: item, busy: model.busy.contains(item.id),
                                             error: model.errors[item.id],
                                             diffMaxHeight: Self.diffMaxHeight(screen: screenHeight),
                                             proposalMaxHeight: Self.proposalMaxHeight(screen: screenHeight)) { approve in
                            Task { await model.decide(item, approve: approve) }
                        }
                    }
                }
            }
            .scrollBounceBehavior(.basedOnSize)
            // More calls than fit: the indicator shows on open that the list scrolls.
            .scrollIndicatorsFlash(onAppear: true)
            .frame(maxHeight: Self.listMaxHeight(screen: screenHeight))
            HStack {
                Spacer()
                Button("Later", action: model.later)
                    .buttonStyle(.looreOutline)
                    .accessibilityHint("Nothing is decided; the changes keep waiting")
                    .accessibilityIdentifier("approvals.later")
            }
        }
        .accessibilityIdentifier("approvals.dialog")
    }
}

/// One waiting call: what it does, Approve and Reject, then the title and
/// description it sets and the diff (or the proposal).
private struct ToolApprovalItemView: View {
    let item: ToolApproval
    let busy: Bool
    let error: String?
    let diffMaxHeight: CGFloat
    let proposalMaxHeight: CGFloat
    let onDecide: (Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(item.title)
                .font(LooreFont.sans(14.7, .regular))
                .foregroundStyle(LooreColor.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            if let versionNote = item.versionNote {
                note(versionNote)
            }
            if let staleNote = item.staleNote {
                note(staleNote)
            }
            if let error {
                Text(error)
                    .font(LooreFont.sans(13.1, .regular))
                    .foregroundStyle(LooreColor.accent)
                    .fixedSize(horizontal: false, vertical: true)
            }
            buttons
            if item.isArtifactWrite {
                // The title and description it changes, above the text's diff.
                ForEach(item.fieldChangeLines, id: \.self) { line in
                    Text(line)
                        .font(LooreFont.sans(13.1, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let failure = item.error, !failure.isEmpty {
                    Text(failure).font(LooreFont.sans(13.1, .light)).foregroundStyle(LooreColor.accent)
                } else if !item.lines.isEmpty {
                    ScrollView {
                        DiffRowsView(diff: VersionDiff(lines: item.lines))
                            .padding(6)
                    }
                    .scrollBounceBehavior(.basedOnSize)
                    .frame(maxHeight: diffMaxHeight)
                    .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: 6))
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
                }
            } else if !item.stale, let text = item.proposalText, !text.isEmpty {
                ScrollView {
                    Text(text)
                        .font(LooreFont.sans(13.1, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .lineSpacing(4)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
                .scrollBounceBehavior(.basedOnSize)
                .frame(maxHeight: proposalMaxHeight)
                .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
            }
        }
        .padding(12)
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(LooreColor.border))
        .accessibilityElement(children: .contain)
    }

    /// Under the call's title, so they never sit below a long diff.
    private var buttons: some View {
        HStack(spacing: 10) {
            Button("Reject") { onDecide(false) }
                .buttonStyle(.looreOutline)
                .disabled(busy)
                .accessibilityIdentifier("approvals.reject.\(item.id)")
            Button("Approve") { onDecide(true) }
                .buttonStyle(.loorePrimary)
                .disabled(busy || !item.canApprove)
                .accessibilityIdentifier("approvals.approve.\(item.id)")
            Spacer(minLength: 0)
        }
    }

    private func note(_ text: String) -> some View {
        Text(text)
            .font(LooreFont.sans(12.5, .light))
            .foregroundStyle(LooreColor.textMuted)
            .fixedSize(horizontal: false, vertical: true)
    }
}
