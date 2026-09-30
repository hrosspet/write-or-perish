import SwiftUI

/// The proposal card under an AI reply (web `ProposalInline`, compact size,
/// map D §6.3–6.5): todo changes with tick / "+", priority order, a GitHub
/// issue, team feedback, shares. Each has one accept button; there is no reject.
struct ProposalCard: View {
    let content: String
    let nodeId: Int
    let toolCallsMeta: [ToolCallMeta]?
    var shareOnly = false
    /// Owner only: the card edits the reply's text (tick, "+") and saves it.
    var onContentChange: ((String) -> Void)?
    /// Keeps the page's `tool_calls_meta` in step after an accept.
    var onApplied: (String, [String: JSONValue]) -> Void = { _, _ in }

    @Environment(AppState.self) private var app
    @State private var applyStatus: String?
    @State private var applyError: String?
    @State private var issueStatus: String?
    @State private var issueError: String?
    @State private var issueURL: String?
    @State private var issueNumber: Int?
    @State private var feedbackStatus: String?
    @State private var feedbackError: String?
    @State private var shareStates: [Int: String] = [:]
    @State private var shareErrors: [Int: String] = [:]
    @State private var metaSavedAll = false
    @State private var metaSavedIndexes: [Int] = []
    @State private var copiedIndex: Int?
    @State private var addingKey: String?
    @State private var addText = ""
    @State private var pollTask: Task<Void, Never>?
    @FocusState private var addFocused: Bool

    private var parsed: ProposalParser.Sections { ProposalParser.parseOrientResponse(content) }
    private var shares: [ProposalParser.ShareBlock] { ProposalParser.parseShareBlocks(content) }

    private var toggleable: Bool {
        onContentChange != nil && applyStatus != "completed" && applyStatus != "started"
    }

    private func meta(_ name: String) -> ToolCallMeta? { toolCallsMeta?.first { $0.name == name } }

    var body: some View {
        let parsed = parsed
        let hasTodo = parsed.completed != nil || parsed.newTasks != nil || parsed.priority != nil
        let hasTodoUpdate = !shareOnly && (hasTodo || meta("propose_todo") != nil)
        let hasIssue = !shareOnly && (parsed.issueTitle != nil && parsed.issueDescription != nil || meta("propose_github_issue") != nil)
        let hasFeedback = !shareOnly && (parsed.feedback != nil || meta("propose_feedback") != nil)
        let shares = shares
        let hasShare = !shares.isEmpty || meta("propose_share") != nil
        let hasPrefs = !shareOnly && (toolCallsMeta?.contains { $0.name == "update_ai_preferences" && $0.status == "success" } ?? false)

        if hasTodoUpdate || hasIssue || hasFeedback || hasShare || hasPrefs {
            VStack(alignment: .leading, spacing: 0) {
                if hasTodo { todoSection(parsed) }
                if hasTodoUpdate { applyTodoArea.padding(.top, 8) }
                if hasIssue, let title = parsed.issueTitle { issueSection(title: title, parsed: parsed) }
                if hasFeedback, let feedback = parsed.feedback { feedbackSection(feedback, category: parsed.feedbackCategory) }
                if hasShare && !shares.isEmpty { shareSection(shares) }
                if hasPrefs {
                    Text("Preferences updated")
                        .font(LooreFont.sans(12, .light))
                        .foregroundStyle(LooreColor.success)
                        .padding(.top, 16)
                }
            }
            .padding(.top, 8)
            .onAppear(perform: readMeta)
            .onChange(of: toolCallsMeta?.count) { _, _ in readMeta() }
            .onDisappear { pollTask?.cancel() }
        }
    }

    // MARK: State from tool_calls_meta

    private func readMeta() {
        guard let toolCallsMeta else { return }
        if let todo = toolCallsMeta.first(where: { $0.name == "propose_todo" }) {
            switch todo.applyStatus {
            case "completed": applyStatus = "completed"
            case "failed":
                applyStatus = "error"
                applyError = todo.applyError ?? "Todo merge failed"
            case "started": applyStatus = "started"
            default: break
            }
        } else if toolCallsMeta.first(where: { $0.name == "apply_todo_changes" })?.status == "success" {
            applyStatus = "completed"
        }
        let issue = toolCallsMeta.first { $0.name == "propose_github_issue" }
        let applyIssue = toolCallsMeta.first { $0.name == "apply_github_issue" }
        if issue?.applyStatus == "completed" {
            issueStatus = "completed"
            issueURL = issue?["issue_url"]?.stringValue
            issueNumber = issue?["issue_number"]?.intValue
        } else if applyIssue?.status == "success" {
            issueStatus = "completed"
            issueURL = applyIssue?["issue_url"]?.stringValue
            issueNumber = applyIssue?["issue_number"]?.intValue
        }
        if toolCallsMeta.first(where: { $0.name == "propose_feedback" })?.applyStatus == "completed"
            || toolCallsMeta.first(where: { $0.name == "apply_feedback" })?.status == "success" {
            feedbackStatus = "completed"
        }
        let share = toolCallsMeta.first { $0.name == "propose_share" }
        if share?.applyStatus == "completed" || toolCallsMeta.first(where: { $0.name == "apply_share" })?.status == "success" {
            metaSavedAll = true
        } else if let indexes = share?["saved_indexes"]?.arrayValue?.compactMap(\.intValue), !indexes.isEmpty {
            metaSavedIndexes = indexes
        }
    }

    // MARK: Todo

    @ViewBuilder
    private func todoSection(_ parsed: ProposalParser.Sections) -> some View {
        if parsed.completed != nil || parsed.newTasks != nil {
            VStack(alignment: .leading, spacing: 0) {
                SectionLabel(text: "Updated from your sharing")
                ForEach(Array(ProposalParser.parseTodoItems(parsed.completed ?? "").enumerated()), id: \.offset) { _, item in
                    taskRow(item, completed: true)
                }
                ForEach(Array(ProposalParser.parseTodoItems(parsed.newTasks ?? "").enumerated()), id: \.offset) { _, item in
                    taskRow(item, completed: false)
                }
            }
            .padding(.bottom, 16)
        }
        if let priority = parsed.priority {
            VStack(alignment: .leading, spacing: 6) {
                SectionLabel(text: "Suggested priority order")
                ForEach(Array(ProposalParser.parsePriorityItems(priority).enumerated()), id: \.offset) { index, item in
                    HStack(spacing: 10) {
                        Text("\(index + 1)")
                            .font(LooreFont.serif(17.6, .regular))
                            .foregroundStyle(LooreColor.accentDim.opacity(0.6))
                            .frame(width: 20)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.text)
                                .font(LooreFont.sans(13.6, .light))
                                .foregroundStyle(LooreColor.textPrimary)
                                .fixedSize(horizontal: false, vertical: true)
                            if !item.hint.isEmpty {
                                Text(item.hint).font(LooreFont.sans(11.5, .light)).foregroundStyle(LooreColor.textMuted)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(.vertical, 10)
                    .padding(.horizontal, 12)
                    .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: 6))
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
                }
            }
            .padding(.bottom, 16)
        }
        if let note = parsed.note, !note.isEmpty {
            (Text("A note: ").foregroundColor(LooreColor.textSecondary) + Text(note))
                .font(LooreFont.sans(13.1, .light))
                .foregroundStyle(LooreColor.textMuted)
                .lineSpacing(6)
                .padding(.bottom, 12.8)
        }
    }

    private func taskRow(_ item: String, completed: Bool) -> some View {
        let key = "\(completed ? "completed" : "new"):\(item)"
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 10) {
                Button {
                    if completed {
                        toggle(item, from: "completed", to: "new task", prepend: true)
                    } else {
                        toggle(item, from: "new task", to: "completed", prepend: false)
                    }
                } label: {
                    ZStack {
                        Circle()
                            .strokeBorder(completed ? LooreColor.accentDim : LooreColor.borderHover, lineWidth: 1.5)
                            .background(Circle().fill(completed ? LooreColor.accentDim : .clear))
                        if completed {
                            Text("✓").font(LooreFont.sans(8.8, .semibold)).foregroundStyle(LooreColor.bgDeep)
                        }
                    }
                    .frame(width: 16, height: 16)
                    .padding(6)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!toggleable)
                .padding(-6)
                .padding(.top, 2)
                .accessibilityLabel(item)
                .accessibilityHint(toggleable ? (completed ? "Unmark as done" : "Mark as done") : "")
                .accessibilityValue(completed ? "Done" : "Not done")
                Text(item)
                    .font(LooreFont.sans(13.6, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                    .strikethrough(completed)
                    .opacity(completed ? 0.4 : 1)
                    .lineSpacing(3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if toggleable {
                    Button {
                        addText = ""
                        addingKey = key
                        addFocused = true
                    } label: {
                        Text("+").font(LooreFont.sans(19, .regular)).foregroundStyle(LooreColor.accent).opacity(0.55)
                            .frame(width: 28, height: 24).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Add an item below")
                }
            }
            .padding(.vertical, 8)
            .overlay(alignment: .bottom) { HairlineDivider() }
            if addingKey == key {
                HStack(spacing: 10) {
                    Circle().strokeBorder(LooreColor.borderHover, style: StrokeStyle(lineWidth: 1.5, dash: [3, 2]))
                        .frame(width: 16, height: 16).opacity(0.5)
                    TextField("", text: $addText, prompt: loorePrompt("New item…"))
                        .font(LooreFont.sans(13.6, .light))
                        .foregroundStyle(LooreColor.textPrimary)
                        .padding(.vertical, 8)
                        .padding(.horizontal, 12)
                        .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
                        .focused($addFocused)
                        .submitLabel(.done)
                        .onSubmit {
                            let text = addText.jsTrimmed
                            if !text.isEmpty { insert(after: item, text) }
                            addingKey = nil
                            addText = ""
                        }
                        .onChange(of: addFocused) { _, focused in
                            if !focused && addText.jsTrimmed.isEmpty { addingKey = nil }
                        }
                }
                .padding(.vertical, 8)
            }
        }
    }

    private func toggle(_ item: String, from: String, to: String, prepend: Bool) {
        guard toggleable, let onContentChange else { return }
        let updated = ProposalParser.moveProposalItem(content, itemText: item, from: from, to: to, prepend: prepend)
        guard updated != content else { return }
        save(updated, previous: content, onContentChange: onContentChange) { "Couldn't save change — reverted (\($0))" }
    }

    private func insert(after item: String, _ text: String) {
        guard toggleable, let onContentChange else { return }
        let updated = MarkdownEdits.insertItemAfter(content, afterItemText: item, newText: text)
        guard updated != content else { return }
        save(updated, previous: content, onContentChange: onContentChange) { "Couldn't add task — reverted (\($0))" }
    }

    private func save(_ updated: String, previous: String, onContentChange: @escaping (String) -> Void,
                      message: @escaping (String) -> String) {
        onContentChange(updated)
        Task {
            do {
                let _: NodeUpdateResponse = try await app.api.put(APIPath.node(nodeId), json: .object(["content": .string(updated)]))
            } catch {
                onContentChange(previous)
                let reason = (error as? APIError)?.serverMessage ?? error.localizedDescription
                app.toasts.show(message(reason))
            }
        }
    }

    private var applyTodoArea: some View {
        Group {
            switch applyStatus {
            case nil:
                ProposalButton(title: "Apply changes to my Todo", action: applyTodo)
            case "started":
                StatusText(text: "Todo update started…", color: LooreColor.textMuted)
            case "completed":
                StatusText(text: "Todo updated", color: LooreColor.success)
            default:
                StatusText(text: applyError ?? "Todo update failed", color: LooreColor.accent)
            }
        }
    }

    /// `POST /api/todo/apply-draft`, then poll `llm-status` every 2 s for the merge.
    private func applyTodo() {
        applyStatus = "started"
        Task {
            do {
                _ = try await app.api.post(APIPath.todoApplyDraft, json: .object(["llm_node_id": .int(nodeId)]),
                                           as: EmptyResponse.self)
                pollApply()
            } catch {
                applyStatus = "error"
                applyError = (error as? APIError)?.userMessage(fallback: "Todo update failed") ?? "Todo update failed"
            }
        }
    }

    private func pollApply() {
        pollTask?.cancel()
        let api = app.api
        pollTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard let status: LLMStatus = try? await api.get(APIPath.llmStatus(nodeId), poll: true) else { continue }
                let todo = status.toolCallsMeta?.first { $0.name == "propose_todo" }
                if todo?.applyStatus == "completed" {
                    applyStatus = "completed"
                    onApplied("propose_todo", ["apply_status": .string("completed")])
                    app.signals.post(.todoChanged)
                    return
                }
                if todo?.applyStatus == "failed" {
                    applyStatus = "error"
                    applyError = todo?.applyError ?? "Todo merge failed"
                    onApplied("propose_todo", ["apply_status": .string("failed"),
                                               "apply_error": .string(todo?.applyError ?? "Todo merge failed")])
                    return
                }
            }
        }
    }

    // MARK: GitHub issue and feedback

    private func issueSection(title: String, parsed: ProposalParser.Sections) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionLabel(text: "GitHub Issue")
            ProposalBox {
                Text(title).font(LooreFont.serif(16, .regular)).foregroundStyle(LooreColor.textPrimary).padding(.bottom, 8)
                if let description = parsed.issueDescription {
                    MarkdownView(markdown: description, style: .proposal)
                }
                if let category = parsed.issueCategory { CategoryBadge(text: category).padding(.top, 6) }
            }
            Group {
                switch issueStatus {
                case nil: ProposalButton(title: "Create issue", action: createIssue)
                case "started": StatusText(text: "Creating issue…", color: LooreColor.textMuted)
                case "completed":
                    HStack(spacing: 0) {
                        StatusText(text: "Issue created", color: LooreColor.success)
                        if let issueURL, let url = URL(string: issueURL) {
                            StatusText(text: " — ", color: LooreColor.success)
                            Button("#\(issueNumber.map(String.init) ?? "")") { app.open(.external(url)) }
                                .buttonStyle(.plain).font(LooreFont.sans(11.5, .regular))
                                .foregroundStyle(LooreColor.success).underline()
                        }
                    }
                default: StatusText(text: issueError ?? "Issue creation failed", color: LooreColor.accent)
                }
            }
            .padding(.top, 8)
        }
        .padding(.top, 12)
    }

    private func createIssue() {
        issueStatus = "started"
        Task {
            do {
                struct Answer: Decodable { var issue_url: String?; var issue_number: Int? }
                let answer: Answer = try await app.api.post(APIPath.githubCreateIssue, json: .object(["llm_node_id": .int(nodeId)]))
                issueStatus = "completed"
                issueURL = answer.issue_url
                issueNumber = answer.issue_number
                onApplied("propose_github_issue", ["apply_status": .string("completed"),
                                                   "issue_url": .optional(answer.issue_url),
                                                   "issue_number": .optional(answer.issue_number)])
            } catch {
                issueStatus = "error"
                issueError = (error as? APIError)?.userMessage(fallback: "Issue creation failed") ?? "Issue creation failed"
            }
        }
    }

    private func feedbackSection(_ feedback: String, category: String?) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionLabel(text: "Feedback")
            ProposalBox {
                MarkdownView(markdown: feedback, style: .proposal)
                if let category { CategoryBadge(text: category).padding(.top, 6) }
            }
            Group {
                switch feedbackStatus {
                case nil: ProposalButton(title: "Send feedback", action: sendFeedback)
                case "started": StatusText(text: "Sending…", color: LooreColor.textMuted)
                case "completed": StatusText(text: "Feedback sent — thank you", color: LooreColor.success)
                default: StatusText(text: feedbackError ?? "Feedback send failed", color: LooreColor.accent)
                }
            }
            .padding(.top, 8)
        }
        .padding(.top, 12)
    }

    private func sendFeedback() {
        feedbackStatus = "started"
        Task {
            do {
                struct Answer: Decodable { var feedback_id: Int? }
                let answer: Answer = try await app.api.post(APIPath.feedbackSubmit, json: .object(["llm_node_id": .int(nodeId)]))
                feedbackStatus = "completed"
                onApplied("propose_feedback", ["apply_status": .string("completed"),
                                               "feedback_id": .optional(answer.feedback_id)])
            } catch {
                feedbackStatus = "error"
                feedbackError = (error as? APIError)?.userMessage(fallback: "Feedback send failed") ?? "Feedback send failed"
            }
        }
    }

    // MARK: Shares

    private func shareSection(_ shares: [ProposalParser.ShareBlock]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionLabel(text: shares.count > 1 ? "Shares" : "Share")
            ForEach(Array(shares.enumerated()), id: \.offset) { index, share in
                VStack(alignment: .leading, spacing: 0) {
                    ProposalBox {
                        MarkdownView(markdown: share.content, style: .proposal)
                            .padding(.trailing, 22)
                        if !share.type.isEmpty { CategoryBadge(text: share.type).padding(.top, 6) }
                    }
                    .overlay(alignment: .topTrailing) {
                        Button {
                            UIPasteboard.general.string = share.content
                            copiedIndex = index
                            Task {
                                try? await Task.sleep(nanoseconds: 1_500_000_000)
                                if copiedIndex == index { copiedIndex = nil }
                            }
                        } label: {
                            Image(systemName: copiedIndex == index ? "checkmark" : "doc.on.doc")
                                .font(.system(size: 12))
                                .foregroundStyle(copiedIndex == index ? LooreColor.success : LooreColor.textMuted)
                                .opacity(copiedIndex == index ? 1 : 0.6)
                                .frame(width: 36, height: 36)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Copy share text")
                    }
                    shareStatus(index).padding(.top, 8)
                }
                .padding(.top, index > 0 ? 14 : 0)
            }
        }
        .padding(.top, 12)
    }

    @ViewBuilder private func shareStatus(_ index: Int) -> some View {
        let state = shareStates[index] ?? ((metaSavedAll || metaSavedIndexes.contains(index)) ? "completed" : nil)
        switch state {
        case nil: ProposalButton(title: "Save to shares") { saveShare(index) }
        case "started": StatusText(text: "Saving…", color: LooreColor.textMuted)
        case "completed":
            HStack(spacing: 0) {
                StatusText(text: "Saved as a private draft", color: LooreColor.success)
                StatusText(text: " — publish from your ", color: LooreColor.textMuted)
                Button("Share page") { app.open(.share) }
                    .buttonStyle(.plain).font(LooreFont.sans(11.5, .regular)).foregroundStyle(LooreColor.accent)
            }
        default: StatusText(text: shareErrors[index] ?? "Saving the share failed", color: LooreColor.accent)
        }
    }

    private func saveShare(_ index: Int) {
        shareStates[index] = "started"
        Task {
            do {
                let answer: JSONValue = try await app.api.post(APIPath.shareSaveProposal, json: .object([
                    "node_id": .int(nodeId), "share_index": .int(index),
                ]))
                shareStates[index] = "completed"
                onApplied("propose_share", [
                    "apply_status": .string(answer["status"]?.stringValue == "completed" ? "completed" : "partial"),
                    "share_id": answer["share"]?["id"] ?? .null,
                    "saved_indexes": answer["saved_indexes"] ?? .null,
                ])
            } catch {
                shareStates[index] = "error"
                shareErrors[index] = (error as? APIError)?.userMessage(fallback: "Saving the share failed")
                    ?? "Saving the share failed"
            }
        }
    }
}

private struct SectionLabel: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(LooreFont.sans(10.9, .regular))
            .tracking(2)
            .foregroundStyle(LooreColor.accent.opacity(0.6))
            .padding(.top, 16)
            .padding(.bottom, 12.8)
    }
}

private struct ProposalButton: View {
    let title: String
    let action: () -> Void

    var body: some View {
        Button(title, action: action)
            .buttonStyle(LooreButtonStyle(kind: .primary, font: LooreFont.sans(12.8, .regular)))
    }
}

private struct StatusText: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text).font(LooreFont.sans(11.5, .regular)).foregroundStyle(color)
    }
}

private struct ProposalBox<Content: View>: View {
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0, content: content)
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
    }
}

private struct CategoryBadge: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(LooreFont.sans(10.9, .regular))
            .tracking(0.9)
            .foregroundStyle(LooreColor.accent)
            .padding(.vertical, 2)
            .padding(.horizontal, 8)
            .overlay(Capsule().strokeBorder(LooreColor.accent))
            .opacity(0.8)
    }
}
