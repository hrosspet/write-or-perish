import SwiftUI

/// The thread screen (`/node/<id>`, web `NodeDetail`, map D §1): ancestors,
/// the focal card with its footer and actions, the inline reply form, and
/// the focal node's whole reply tree.
struct ThreadView: View {
    let nodeId: Int
    let awaitLLM: Int?

    @Environment(AppState.self) private var app
    @State private var model: ThreadModel?
    @State private var appearedBefore = false
    @AppStorage(DefaultsKey.autoGenerate) private var autoGenerate = true

    var body: some View {
        Group {
            if let model {
                ThreadContent(model: model, autoGenerate: $autoGenerate)
            } else {
                LoadingLine(text: "Loading node...")
            }
        }
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
        .navigationTitle(model?.tabTitle ?? "Thread")
        .toolbar { ToolbarItem(placement: .principal) { Text("") } }
        .task {
            if model == nil {
                let m = ThreadModel(nodeId: nodeId, awaitLLM: awaitLLM, app: app)
                model = m
                await m.start()
            }
        }
        .onAppear {
            if appearedBefore, let model { Task { await model.reload() } }
            appearedBefore = true
        }
        .onDisappear { model?.stop() }
    }
}

private struct ThreadContent: View {
    @Bindable var model: ThreadModel
    @Binding var autoGenerate: Bool
    @Environment(AppState.self) private var app
    @State private var scrolledToFocal = false
    @State private var formToken = 0

    private var craftMode: Bool { app.capabilities.craftMode }

    var body: some View {
        if model.loading {
            LoadingLine(text: "Loading node...")
        } else if let error = model.pageError {
            ScrollView { ErrorLine(text: error).padding(.horizontal, LooreSpacing.gutter) }
        } else if let node = model.node {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        header(node)
                        ForEach(node.ancestors) { ancestor in
                            BubbleView(data: BubbleData(ancestor), actions: actions(model.target(ancestor),
                                       userId: ancestor.userId, parentUserId: ancestor.parentUserId, nodeType: ancestor.nodeType)) {
                                app.open(.thread(id: ancestor.id, awaitLLM: nil))
                            }
                            .padding(.vertical, 8)
                        }
                        HairlineDivider().padding(.top, node.ancestors.isEmpty ? 0 : 2)
                            .id("focal")
                        focalSection(node)
                        HairlineDivider().padding(.vertical, 8)
                        ForEach(ChildRow.flatten(node.children)) { row in
                            ChildRowView(row: row) { child in
                                AnyView(BubbleView(data: BubbleData(child),
                                                   actions: actions(model.target(child), userId: child.userId,
                                                                    parentUserId: child.parentUserId, nodeType: child.nodeType)) {
                                    app.open(.thread(id: child.id, awaitLLM: nil))
                                })
                            }
                        }
                    }
                    .padding(.horizontal, 12)
                    .padding(.top, 8)
                    .padding(.bottom, 48)
                    .looreReadableWidth()
                }
                .scrollDismissesKeyboard(.interactively)
                .task(id: node.id) {
                    // Once per focal node, after the first layout (web: scrollIntoView, block start).
                    guard !scrolledToFocal, !node.ancestors.isEmpty else { return }
                    scrolledToFocal = true
                    try? await Task.sleep(nanoseconds: 350_000_000)
                    withAnimation(LooreMotion.quick) { proxy.scrollTo("focal", anchor: .top) }
                }
            }
            .modifier(ThreadSheets(model: model, autoGenerate: $autoGenerate))
        }
    }

    private func actions(_ target: NodeTarget, userId: Int?, parentUserId: Int?, nodeType: NodeType) -> [BubbleAction] {
        var list = [BubbleAction(label: "Reply", kind: .reply) { model.beginReply(target) }]
        if model.ownedByMe(userId: userId, parentUserId: parentUserId, nodeType: nodeType) {
            list.append(BubbleAction(label: "Edit") { model.beginEdit(target) })
            list.append(BubbleAction(label: "Delete", destructive: true) { model.beginDelete(target) })
        }
        return list
    }

    // MARK: Header

    private func header(_ node: NodeDetail) -> some View {
        HStack(alignment: .bottom, spacing: 16) {
            Text("Thread")
                .font(LooreFont.serif(28.8, .light, relativeTo: .largeTitle))
                .foregroundStyle(LooreColor.textPrimary)
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 8)
            if model.isOwner && node.aiUsage != .off && !model.isPublicThread {
                VStack(alignment: .trailing, spacing: 6) {
                    TopRightButton(title: model.voiceLoading ? "Starting…" : "Voice Mode") {
                        Image(systemName: "mic.fill").font(.system(size: 11))
                    } action: { model.startVoice() }
                    .disabled(model.voiceLoading)
                    .accessibilityHint("Continue this conversation by voice")
                    if craftMode {
                        TopRightButton(title: "Auto-generate") {
                            LoorePillSwitch(isOn: autoGenerate)
                        } action: { autoGenerate.toggle() }
                        .accessibilityValue(autoGenerate ? "On" : "Off")
                        .accessibilityHint(autoGenerate ? "Auto-generate is on — tap to turn off"
                                           : "Auto-generate is off — tap to turn on")
                    }
                }
            }
        }
        .padding(.bottom, 12)
    }

    // MARK: Focal section

    @ViewBuilder
    private func focalSection(_ node: NodeDetail) -> some View {
        let showCraftBar = model.showCraftBar(craftMode: craftMode, autoGenerate: autoGenerate)
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 4) {
                FocalCard(model: model, node: node)
                if model.isOwner {
                    KebabMenu(actions: [
                        BubbleAction(label: "Edit") { model.beginEdit(model.target(focal: node)) },
                        BubbleAction(label: "Delete", destructive: true) { model.beginDelete(model.target(focal: node)) },
                    ])
                } else {
                    Color.clear.frame(width: KebabMenu.width)
                }
            }
            .padding(.top, 18)
            .padding(.bottom, 10)
            VStack(alignment: .leading, spacing: 0) {
                NodeFooterView(username: node.authorUsername, createdAt: node.createdAt, childCount: node.childCount,
                               humanOwnerUsername: humanOwner(node), llmModel: node.llmModel, origin: node.origin,
                               isPublic: node.privacyLevel == .public) {
                    Button(action: model.togglePin) {
                        Image(systemName: node.pinnedAt != nil ? "pin.fill" : "pin")
                            .font(.system(size: 11))
                            .foregroundStyle(node.pinnedAt != nil ? LooreColor.accent : LooreColor.textMuted)
                            .frame(minWidth: 24, minHeight: 28)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!model.canPin || model.pinLoading)
                    .opacity(model.canPin ? 1 : 0.35)
                    .accessibilityLabel(model.pinTitle)
                    NodeAudioControls(nodeId: node.id, content: node.content, isPublic: node.privacyLevel == .public,
                                      aiUsage: node.aiUsage, hasTTS: node.hasTTS)
                }
                if showCraftBar && !(model.inReadThread && !model.readReplyAbove) {
                    llmResponseRow(node).padding(.top, 8)
                }
                if model.llmTaskNodeId != nil && !showCraftBar && !model.isLLMPending {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.mini).tint(LooreColor.textMuted)
                        Text(model.llmPollStatus == .pending ? "Waiting for AI…" : "Generating…")
                    }
                    .font(LooreFont.sans(12.5, .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .padding(.top, 8)
                }
            }
            .padding(.bottom, 10)
            NodeFormView(config: inlineConfig(node)) { result in
                formToken += 1
                if model.isReadReply {
                    model.readReplySent(result)
                } else {
                    Task { await model.inlineReplySent(result, autoGenerate: $autoGenerate) }
                }
            }
            .id(formToken)
            .padding(.top, 4)
            .padding(.bottom, 12)
        }
        .padding(.leading, 8)
    }

    private func inlineConfig(_ node: NodeDetail) -> NodeFormConfig {
        var config = NodeFormConfig(parentId: node.id, hidePowerFeatures: !craftMode, hideAudioUpload: !craftMode,
                                    compact: true,
                                    placeholder: model.isReadReply ? "Ask about these picks, or say what you make of them…"
                                        : "Type what's on your mind…")
        if model.isReadReply {
            config.submitOverride = { [model, autoGenerate] submission in
                try await model.submitReadReply(submission, autoGenerate: autoGenerate)
            }
        }
        return config
    }

    /// An AI reply's human owner (walk the ancestors for the nearest user node).
    private func humanOwner(_ node: NodeDetail) -> String? {
        guard node.nodeType == .llm, node.parentUserId != nil else { return nil }
        return node.ancestors.last(where: { $0.nodeType != .llm })?.username
    }

    private func llmResponseRow(_ node: NodeDetail) -> some View {
        let busy = model.llmRequesting || model.llmTaskNodeId != nil
        let underReadReply = model.isReadReply && node.llmTaskStatus == .completed
        return HStack(spacing: 0) {
            Button(action: model.llmResponsePressed) {
                HStack(spacing: 8) {
                    if busy { ProgressView().controlSize(.mini).tint(LooreColor.textSecondary) }
                    Text(model.llmRequesting ? "Requesting…"
                         : model.llmTaskNodeId != nil ? (model.llmPollStatus == .pending ? "Waiting for AI…" : "Generating…")
                         : "LLM Response")
                }
                .font(LooreFont.button)
                .foregroundStyle(LooreColor.textSecondary)
                .padding(.vertical, 10)
                .padding(.horizontal, 16)
                .overlay(UnevenRoundedRectangle(topLeadingRadius: LooreRadius.control, bottomLeadingRadius: LooreRadius.control)
                    .strokeBorder(LooreColor.border))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(busy || underReadReply)
            .opacity(busy || underReadReply ? 0.45 : 1)
            .accessibilityIdentifier("thread.llmResponse")
            ModelPicker(nodeId: node.id, selectedModel: $model.selectedModel, disabled: busy || underReadReply)
                .frame(maxWidth: 200)
                .padding(.leading, -1)
            Spacer(minLength: 0)
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

/// The small top-right buttons (Voice Mode, Auto-generate): 160×32.
private struct TopRightButton<Trailing: View>: View {
    let title: String
    @ViewBuilder var trailing: () -> Trailing
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack {
                Text(title)
                Spacer(minLength: 4)
                trailing().frame(width: 32)
            }
            .font(LooreFont.sans(12.5, .light))
            .foregroundStyle(LooreColor.textMuted)
            .padding(.horizontal, 12)
            .frame(width: 160, height: 32)
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Focal card

private struct FocalCard: View {
    @Bindable var model: ThreadModel
    let node: NodeDetail
    @Environment(AppState.self) private var app

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if node.systemPrompt.isSystemPrompt, let title = node.systemPrompt.promptTitle {
                HStack(spacing: 6) {
                    Text(title + (node.systemPrompt.promptVersionNumber.map { " v\($0)" } ?? ""))
                    if let v = node.systemPrompt.contextArtifacts?.profile {
                        Text("· Profile v\(v.versionNumber.map(String.init) ?? "")").opacity(0.7)
                    }
                    if let v = node.systemPrompt.contextArtifacts?.todo {
                        Text("· TODO v\(v.versionNumber.map(String.init) ?? "")").opacity(0.7)
                    }
                }
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(LooreColor.textMuted)
                .padding(.bottom, 9.6)
            }
            content
            if let meta = node.toolCallsMeta?.filter({ !$0.isInternal }), !meta.isEmpty {
                ToolCallsDisclosure(meta: meta, expanded: $model.toolActionsExpanded)
            }
        }
        .padding(.vertical, 28.8)
        .padding(.leading, 32)
        .padding(.trailing, 28)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.card))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.card).strokeBorder(LooreColor.border))
        .overlay(alignment: .leading) {
            UnevenRoundedRectangle(topLeadingRadius: LooreRadius.card, bottomLeadingRadius: LooreRadius.card)
                .fill(LooreColor.accent).frame(width: 3)
        }
        .accessibilityIdentifier("thread.focal")
    }

    @ViewBuilder private var content: some View {
        let partial = ContentSegmenter.partialReplyText(model.streamText)
        if model.isLLMPending && !partial.jsTrimmed.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                QuotedContentView(content: partial, contextArtifacts: node.systemPrompt.contextArtifacts, nodeId: node.id)
                PulsingDots().padding(.vertical, 6).accessibilityLabel("Still writing")
            }
        } else if model.isLLMPending {
            HStack(spacing: 10) {
                Text(model.isBatchWait ? "Processing" : "Thinking")
                    .font(LooreFont.sansOblique(15.2, .light))
                    .foregroundStyle(LooreColor.textMuted)
                PulsingDots()
            }
            .padding(.vertical, 8)
        } else {
            let showProposal = model.showProposal
            let split = showProposal ? ProposalParser.splitProposalText(node.content, shareOnly: !model.isLLMNode) : nil
            let display = split?.before ?? node.content
            if !showProposal || !display.isEmpty {
                QuotedContentView(
                    content: display, quotes: model.quotes, contextArtifacts: node.systemPrompt.contextArtifacts,
                    nodeId: node.id,
                    checklist: model.isOwner ? ChecklistActions(toggle: { model.toggleCheckbox($0, checked: $1) },
                                                                add: { model.insertTask(after: $0, $1) }) : nil,
                    onExternalReadChange: { model.updateExternalRead($0, readAt: $1) },
                    onExternalFeedbackChange: { model.updateExternalFeedback($0, feedback: $1) })
            }
            if showProposal {
                ProposalCard(content: node.content, nodeId: node.id, toolCallsMeta: node.toolCallsMeta,
                             shareOnly: !model.isLLMNode,
                             onContentChange: model.isOwner ? { model.setContent($0) } : nil,
                             onApplied: { model.updateToolMeta($0, $1) })
                if let after = split?.after, !after.isEmpty {
                    QuotedContentView(content: after, quotes: model.quotes,
                                      contextArtifacts: node.systemPrompt.contextArtifacts, nodeId: node.id)
                        .padding(.top, 20)
                }
            }
        }
    }
}

/// Three 5pt dots pulsing in turn (1.2 s cycle, 0.15 s stagger).
struct PulsingDots: View {
    @State private var phase = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: reduceMotion)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            HStack(spacing: 3) {
                ForEach(0..<3, id: \.self) { i in
                    let local = (t - Double(i) * 0.15).truncatingRemainder(dividingBy: 1.2) / 1.2
                    let peak = local < 0.3 ? local / 0.3 : (local < 0.6 ? (0.6 - local) / 0.3 : 0)
                    Circle()
                        .fill(LooreColor.textMuted)
                        .frame(width: 5, height: 5)
                        .opacity(reduceMotion ? 0.6 : 0.3 + 0.7 * max(0, peak))
                        .offset(y: reduceMotion ? 0 : -2 * max(0, peak))
                }
            }
        }
        .frame(height: 9)
    }
}

/// "▸ Actions taken (N)" and the tool-call rows (map D §2.3).
private struct ToolCallsDisclosure: View {
    let meta: [ToolCallMeta]
    @Binding var expanded: Bool
    @Environment(AppState.self) private var app

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                expanded.toggle()
            } label: {
                Text("\(expanded ? "▾" : "▸") Actions taken (\(meta.count))")
                    .font(LooreFont.sans(12, .light))
                    .foregroundStyle(LooreColor.textMuted)
            }
            .buttonStyle(.plain)
            if expanded {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(meta.enumerated()), id: \.offset) { _, tc in
                        row(tc)
                    }
                }
            }
        }
        .padding(.top, 8)
        .overlay(alignment: .top) { HairlineDivider() }
        .padding(.top, 12)
    }

    private func row(_ tc: ToolCallMeta) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Text(tc.status == "success" ? "✓" : "✗")
            label(tc)
            if let err = tc["error"]?.stringValue, !err.isEmpty {
                Text(" — \(err)").foregroundStyle(LooreColor.accent)
            }
            Spacer(minLength: 0)
        }
        .font(LooreFont.sans(12.5, .light))
        .foregroundStyle(LooreColor.textSecondary)
        .padding(.vertical, 6)
        .padding(.horizontal, 10)
        .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
    }

    @ViewBuilder private func label(_ tc: ToolCallMeta) -> some View {
        let apply = tc.applyStatus
        let kind = tc["kind"]?.stringValue
        switch tc.name {
        case "propose_todo":
            Text("Todo update proposed" + (apply == "completed" ? " (applied)" : apply == "started" ? " (applying...)"
                                           : apply == "failed" ? " (failed)" : ""))
        case "propose_github_issue":
            Text("Issue proposed" + (apply == "completed" ? " (created)" : apply == "failed" ? " (failed)" : ""))
        case "propose_feedback":
            Text("Feedback proposed" + (apply == "completed" ? " (sent)" : apply == "failed" ? " (failed)" : ""))
        case "propose_share":
            Text("Share proposed" + (apply == "completed" ? " (saved as draft)" : apply == "failed" ? " (failed)" : ""))
        case "apply_todo_changes":
            Text(tc.status != "success" ? "Todo apply failed"
                 : apply == "completed" ? "Todo changes applied"
                 : apply == "failed" ? "Todo apply failed" + (tc.applyError.map { ": " + $0 } ?? "")
                 : "Todo apply in progress...")
        case "apply_github_issue":
            Text(tc.status == "success" ? "Issue creation confirmed" : "Issue creation failed")
        case "apply_feedback":
            Text(tc.status == "success" ? "Feedback sent" : "Feedback send failed")
        case "apply_share":
            if tc.status == "success" {
                HStack(spacing: 0) {
                    Text("Share saved as a draft — ")
                    linkButton("Share page") { app.open(.share) }
                }
            } else {
                Text("Share save failed")
            }
        case "update_ai_preferences":
            Text("Preferences updated")
        case "update_artifact", "read_artifact":
            HStack(spacing: 4) {
                Text(tc.name == "read_artifact" ? "Read artifact" : ((tc["created"]?.boolValue ?? false) ? "Created artifact" : "Updated artifact"))
                if let kind {
                    linkButton(kind, mono: true) { app.open(.artifacts(kind: kind)) }
                }
            }
        case "read_todo":
            HStack(spacing: 4) {
                Text("Read")
                linkButton("todo list") { app.open(.todo) }
            }
        case "semantic_search":
            Text("Searched archive & references" + (tc["query"]?.stringValue.map { " — “\($0)”" } ?? ""))
        case "read_full":
            if tc.status != "success" {
                Text("Read in full (failed)")
            } else if kind == "external" {
                if let url = tc["url"]?.stringValue.flatMap(URL.init(string:)) {
                    HStack(spacing: 0) {
                        Text("Read in full — ")
                        linkButton(tc["author_handle"]?.stringValue.map { "@\($0)'s post" } ?? "saved reference") {
                            app.open(.external(url))
                        }
                    }
                } else {
                    Text("Read a saved reference in full")
                }
            } else {
                let ref = tc["ref_id"]?.intValue
                HStack(spacing: 0) {
                    Text("Read in full — ")
                    linkButton("entry #\(ref.map(String.init) ?? "")") {
                        if let ref { app.open(.thread(id: ref, awaitLLM: nil)) }
                    }
                }
            }
        default:
            Text(tc.name)
        }
    }

    private func linkButton(_ title: String, mono: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(mono ? MarkdownFonts.mono(12) : LooreFont.sans(12.5, .light))
                .foregroundStyle(LooreColor.accent)
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Reply tree

/// One row of the flattened reply tree: a bubble or the rule between siblings.
/// `guides` says, for each ancestor level, whether that level's siblings are
/// indented with a left rule (the web's `RenderChildTree`: only when > 1 sibling).
struct ChildRow: Identifiable {
    enum Kind { case bubble(TreeNode), separator }
    let id: String
    let kind: Kind
    let guides: [Bool]

    static func flatten(_ nodes: [TreeNode], guides: [Bool] = []) -> [ChildRow] {
        var rows: [ChildRow] = []
        let indented = nodes.count > 1
        for (i, child) in nodes.enumerated() {
            let own = guides + [indented]
            rows.append(ChildRow(id: "n\(child.id)", kind: .bubble(child), guides: own))
            rows.append(contentsOf: flatten(child.children, guides: own))
            if i < nodes.count - 1 {
                rows.append(ChildRow(id: "s\(child.id)", kind: .separator, guides: own))
            }
        }
        return rows
    }
}

private struct ChildRowView: View {
    let row: ChildRow
    let bubble: (TreeNode) -> AnyView

    /// Indent per nested sibling level: margin, 2pt rule, padding (web 20 + 2 + 10).
    static let margin: CGFloat = 14
    static let padding: CGFloat = 8

    private var isSeparator: Bool {
        if case .separator = row.kind { return true }
        return false
    }

    private var indent: CGFloat {
        row.guides.enumerated().reduce(0) { total, level in
            guard level.element else { return total }
            let last = level.offset == row.guides.count - 1
            return total + (isSeparator && last ? Self.margin : Self.margin + 2 + Self.padding)
        }
    }

    var body: some View {
        Group {
            switch row.kind {
            case .bubble(let node):
                bubble(node).padding(.vertical, 8)
            case .separator:
                HairlineDivider().padding(.vertical, 8)
            }
        }
        .padding(.leading, indent)
        .background(alignment: .leading) {
            HStack(spacing: 0) {
                ForEach(Array(row.guides.enumerated()), id: \.offset) { index, indented in
                    if indented {
                        Color.clear.frame(width: Self.margin)
                        if !(isSeparator && index == row.guides.count - 1) {
                            Rectangle().fill(LooreColor.border).frame(width: 2)
                            Color.clear.frame(width: Self.padding)
                        }
                    }
                }
            }
            .accessibilityHidden(true)
        }
    }
}
